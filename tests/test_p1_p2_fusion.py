# filepath: tests/test_p1_p2_fusion.py
"""P1/P2 单元测试（2026-08-19）

覆盖：
1. P1-2 factor_collector 个股监控：_stock_symbol / load_watchlist_stocks /
   fetch_stock_quotes / detect_stock_anomalies / monitor_stocks（空名单）
2. P1-3 资金流：fetch_market_flows（mock 两数据源）/ detect_flow_anomalies /
   build_snapshot 新增键 / format_snapshot 新区块
3. P2-2 LLM 环境注入：_llm_env_context / _build_llm_user_prompt 头部 /
   _llm_judge env_note 解析 / format_push_alert 环境行 / _factor_env_line 资金流
4. P1-2 资讯引用：factor_collector._related_pushed_news 关键词过滤
5. P2-3 signal_backtest：_forward_returns / backtest 合成事件 / build_report
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
import signal_backtest as sb  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单元测试：mock 数据源，无网络无推送

BJT = timezone(timedelta(hours=8))


def _sample_snapshot(ts=None, with_flows=True) -> dict:
    snap = {
        "ts": ts or datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
        "risk_state": "risk_off",
        "indexes": {
            "上证指数": {"price": 3894.42, "change_pct": -2.40, "trend": "均线纠缠"},
            "创业板指": {"price": 3473.49, "change_pct": -6.26, "trend": "均线纠缠"},
        },
        "basis": {
            "IC": {"basis_pct": -0.92, "annual_pct": -11.17},
            "IM": {"basis_pct": -0.65, "annual_pct": -7.93},
        },
        "fx": {"美元/日元": {"price": 159.19, "change_pct": -0.43}},
    }
    if with_flows:
        snap["flows"] = {"main_net_yi": -1939.6, "margin_yi": 26708.7, "margin_chg_yi": 52.0}
    return snap


# ============================================================
# P1-2: _stock_symbol / load_watchlist_stocks / fetch_stock_quotes
# ============================================================

class TestStockSymbol:
    def test_prefix_rules(self):
        assert fc._stock_symbol("300308") == "sz300308"
        assert fc._stock_symbol("301377") == "sz301377"
        assert fc._stock_symbol("688498") == "sh688498"
        assert fc._stock_symbol("600183") == "sh600183"
        assert fc._stock_symbol("000636") == "sz000636"
        assert fc._stock_symbol("830799") == "bj830799"

    def test_prefixed_passthrough(self):
        assert fc._stock_symbol("sh600519") == "sh600519"
        assert fc._stock_symbol("SZ000001") == "sz000001"


class TestLoadWatchlistStocks:
    def test_dict_entries_with_code_kept(self, monkeypatch, tmp_path):
        wl = tmp_path / "watchlist.json"
        wl.write_text(json.dumps({
            "stocks": [
                {"name": "中际旭创", "code": "300308"},
                "纯名称条目",
                {"name": "缺代码"},
                {"code": "300502"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(fc, "WATCHLIST_PATH", wl)
        out = fc.load_watchlist_stocks()
        assert out == [{"name": "中际旭创", "code": "300308", "symbol": "sz300308"}]

    def test_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fc, "WATCHLIST_PATH", tmp_path / "nope.json")
        assert fc.load_watchlist_stocks() == []


class TestFetchStockQuotes:
    def test_parse_tencent_payload(self, monkeypatch):
        # 真实接口字段位：1=名称 3=现价 4=昨收 32=涨跌%；总字段数 50+
        parts = ["1", "中际旭创", "300308", "895.60", "988.00"] + [""] * 27 + ["-9.36"]
        parts += [""] * 5  # 补齐到 38 字段（覆盖 len(parts) >= 38 门槛）
        payload = "~".join(parts)
        text = f'v_sz300308="{payload}";'
        monkeypatch.setattr(fc, "_http_get", lambda url, params=None, headers=None, encoding=None: text)
        out = fc.fetch_stock_quotes(["sz300308"])
        assert "sz300308" in out
        q = out["sz300308"]
        assert q["name"] == "中际旭创"
        assert q["price"] == 895.60
        assert q["change_pct"] == -9.36

    def test_empty_symbols(self):
        assert fc.fetch_stock_quotes([]) == {}


# ============================================================
# P1-2: detect_stock_anomalies / monitor_stocks
# ============================================================

def _stock_tech(chg=0.0, vol=1.0, breakout=False, breakdown=False, available=True):
    return {
        "available": available, "price": 100.0, "change_pct": chg,
        "trend": "均线纠缠", "vol_ratio5": vol,
        "breakout": breakout, "breakdown": breakdown, "code": "300308",
    }


class TestDetectStockAnomalies:
    def test_big_drop_triggers_chg_signal(self):
        sigs = fc.detect_stock_anomalies({"中际旭创": _stock_tech(chg=-9.36)})
        assert len(sigs) == 1
        s = sigs[0]
        assert s["key"] == "stock_chg_300308"
        assert s["direction"] == "bearish"
        assert s["level"] == "warning"
        assert s["stock"] == "中际旭创"

    def test_big_rise_is_info_level(self):
        sigs = fc.detect_stock_anomalies({"中际旭创": _stock_tech(chg=6.2)})
        assert sigs[0]["level"] == "info"
        assert sigs[0]["direction"] == "bullish"

    def test_high_vol_ratio_signal(self):
        sigs = fc.detect_stock_anomalies({"中际旭创": _stock_tech(chg=1.0, vol=3.0)})
        assert len(sigs) == 1
        assert sigs[0]["key"] == "stock_vol_300308"

    def test_breakdown_with_volume(self):
        sigs = fc.detect_stock_anomalies({"中际旭创": _stock_tech(chg=-3.0, vol=1.6, breakdown=True)})
        assert len(sigs) == 1
        assert sigs[0]["key"] == "stock_brk_300308"
        assert sigs[0]["direction"] == "bearish"

    def test_single_signal_per_stock_priority(self):
        # 跌幅与量比同时命中：只报跌幅（优先级最高）
        sigs = fc.detect_stock_anomalies({"中际旭创": _stock_tech(chg=-8.0, vol=3.0)})
        assert len(sigs) == 1
        assert sigs[0]["key"] == "stock_chg_300308"

    def test_quiet_stock_no_signal(self):
        assert fc.detect_stock_anomalies({"中际旭创": _stock_tech(chg=-4.82, vol=1.09)}) == []

    def test_unavailable_skipped(self):
        assert fc.detect_stock_anomalies({"中际旭创": _stock_tech(available=False)}) == []

    def test_below_chg_threshold_not_triggered(self):
        # 4.99% 不触发（阈值含等于 5.0）
        assert fc.detect_stock_anomalies({"中际旭创": _stock_tech(chg=4.99)}) == []
        assert len(fc.detect_stock_anomalies({"中际旭创": _stock_tech(chg=5.0)})) == 1


class TestMonitorStocks:
    def test_empty_watchlist_returns_empty(self, monkeypatch, tmp_path):
        wl = tmp_path / "watchlist.json"
        wl.write_text(json.dumps({"stocks": []}), encoding="utf-8")
        monkeypatch.setattr(fc, "WATCHLIST_PATH", wl)
        assert fc.monitor_stocks() == ([], {}, {})


# ============================================================
# P1-3: fetch_market_flows / detect_flow_anomalies
# ============================================================

class TestFetchMarketFlows:
    def test_both_sources_parsed(self, monkeypatch):
        kline_row = "2026-08-19,-193963880448.0,146103312384.0,47860572160.0,-80929013760.0,-113034866688.0"
        fflow = json.dumps({"data": {"klines": [kline_row]}})
        margin = json.dumps({"result": {"data": [
            {"DIM_DATE": "2026-08-18", "RZYE": 2670868909392},
            {"DIM_DATE": "2026-08-17", "RZYE": 2665670938861},
        ]}})
        calls = {"n": 0}

        def fake_get(url, params=None, headers=None, encoding=None):
            calls["n"] += 1
            return fflow if "fflow" in url else margin

        monkeypatch.setattr(fc, "_http_get", fake_get)
        out = fc.fetch_market_flows()
        assert out["main_net_yi"] == -1939.6
        assert out["margin_yi"] == 26708.7
        assert out["margin_chg_yi"] == 52.0

    def test_partial_failure_tolerated(self, monkeypatch):
        # fflow 源失败（空响应），融资余额源正常 → 只返回 margin 字段
        margin = json.dumps({"result": {"data": [
            {"DIM_DATE": "2026-08-18", "RZYE": 2670868909392},
            {"DIM_DATE": "2026-08-17", "RZYE": 2665670938861},
        ]}})

        def fake_get(url, params=None, headers=None, encoding=None):
            return "" if "fflow" in url else margin

        monkeypatch.setattr(fc, "_http_get", fake_get)
        out = fc.fetch_market_flows()
        assert "main_net_yi" not in out
        assert out["margin_yi"] == 26708.7


class TestDetectFlowAnomalies:
    def test_big_outflow_is_warning_bearish(self):
        sigs = fc.detect_flow_anomalies({"main_net_yi": -1939.6})
        assert len(sigs) == 1
        assert sigs[0]["key"] == "flow_main_net"
        assert sigs[0]["level"] == "warning"
        assert sigs[0]["direction"] == "bearish"

    def test_big_inflow_is_info_bullish(self):
        sigs = fc.detect_flow_anomalies({"main_net_yi": 350.0})
        assert sigs[0]["level"] == "info"
        assert sigs[0]["direction"] == "bullish"

    def test_margin_change_signal(self):
        sigs = fc.detect_flow_anomalies({"margin_yi": 26708.0, "margin_chg_yi": -90.0})
        assert len(sigs) == 1
        assert sigs[0]["key"] == "flow_margin"
        assert sigs[0]["direction"] == "bearish"

    def test_below_thresholds_no_signal(self):
        assert fc.detect_flow_anomalies({"main_net_yi": -200.0, "margin_chg_yi": 50.0}) == []


class TestSnapshotP1Fields:
    def test_build_snapshot_with_stocks_and_flows(self):
        snap = fc.build_snapshot({}, {}, {}, "neutral",
                                 stocks={"中际旭创": {"code": "300308", "price": 895.6, "change_pct": -9.36}},
                                 flows={"main_net_yi": -1939.6, "margin_yi": 26708.7, "margin_chg_yi": 52.0})
        assert snap["stocks"]["中际旭创"]["price"] == 895.6
        assert snap["flows"]["main_net_yi"] == -1939.6

    def test_build_snapshot_without_optionals_keeps_old_shape(self):
        snap = fc.build_snapshot({}, {}, {}, "neutral")
        assert "stocks" not in snap
        assert "flows" not in snap
        assert set(snap) == {"ts", "risk_state", "indexes", "basis", "fx"}

    def test_format_snapshot_new_sections(self):
        text = fc.format_snapshot(
            {}, {}, {},
            stocks={"中际旭创": {"available": True, "price": 895.6, "change_pct": -9.36,
                             "trend": "均线纠缠", "vol_ratio5": 1.29, "code": "300308"}},
            flows={"main_net_yi": -1939.6, "margin_yi": 26708.7, "margin_chg_yi": 52.0},
        )
        assert "### 资金面" in text
        assert "主力净流出 1940 亿" in text
        assert "融资余额 26709 亿" in text
        assert "### 自选股" in text
        assert "中际旭创(300308) 895.60" in text


# ============================================================
# P1-2: _related_pushed_news（个股异动卡片附资讯引用）
# ============================================================

class TestRelatedPushedNews:
    def _write_state(self, tmp_path, rows):
        state = {"seen": {f"fp{i}": rec for i, rec in enumerate(rows)}}
        p = tmp_path / "real_time_state.json"
        p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        return p

    def test_keyword_filter_and_limit(self, monkeypatch, tmp_path):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        p = self._write_state(tmp_path, [
            {"pushed": True, "t": now, "title": "中际旭创获大额订单"},
            {"pushed": True, "t": now, "title": "央行开展逆回购操作"},
            {"pushed": True, "t": now, "title": "新易盛业绩预增"},
            {"pushed": False, "t": now, "title": "中际旭创机构调研"},
        ])
        monkeypatch.setattr(fc, "_REALTIME_STATE_PATH", p)
        monkeypatch.delenv("GIST_TOKEN", raising=False)
        out = fc._related_pushed_news(["中际旭创", "300308"], hours=48)
        assert out == ["中际旭创获大额订单"]

    def test_no_match_returns_empty(self, monkeypatch, tmp_path):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        p = self._write_state(tmp_path, [{"pushed": True, "t": now, "title": "央行降准"}])
        monkeypatch.setattr(fc, "_REALTIME_STATE_PATH", p)
        monkeypatch.delenv("GIST_TOKEN", raising=False)
        assert fc._related_pushed_news(["中际旭创"]) == []

    def test_empty_keywords(self):
        assert fc._related_pushed_news([]) == []


# ============================================================
# P2-2: _llm_env_context / _build_llm_user_prompt / env_note
# ============================================================

class TestLlmEnvContext:
    def test_full_snapshot(self):
        ctx = rtp._llm_env_context(_sample_snapshot())
        assert "风险收缩期" in ctx
        assert "IC贴水-0.92%" in ctx
        assert "上证指数-2.40%" in ctx
        assert "主力净流出1940亿" in ctx

    def test_stale_snapshot_returns_empty(self):
        old = (datetime.now(BJT) - timedelta(hours=50)).strftime("%Y-%m-%d %H:%M")
        assert rtp._llm_env_context(_sample_snapshot(ts=old)) == ""

    def test_empty_snapshot_returns_empty(self):
        assert rtp._llm_env_context({}) == ""

    def test_no_flows_key_backward_compatible(self):
        ctx = rtp._llm_env_context(_sample_snapshot(with_flows=False))
        assert "主力" not in ctx
        assert "风险收缩期" in ctx


class TestBuildLlmUserPrompt:
    def test_env_context_header(self):
        items = [{"_judge_idx": 0, "title": "测试", "content": "内容", "published_at": ""}]
        out = rtp._build_llm_user_prompt(items, env_context="风险收缩期")
        assert out.startswith("【当前量化环境】风险收缩期")

    def test_no_env_context_no_header(self):
        items = [{"_judge_idx": 0, "title": "测试", "content": "内容", "published_at": ""}]
        out = rtp._build_llm_user_prompt(items)
        assert "当前量化环境" not in out
        assert out.startswith("请逐条审核以下资讯")


class TestLlmJudgeEnvNote:
    def test_env_note_parsed(self, monkeypatch):
        raw = json.dumps([{
            "idx": 0, "title": "测试", "push": True, "score": 8,
            "direction": "bullish", "scope": "sector", "sectors": ["AI算力"],
            "entities": [], "is_leader_stock": False,
            "env_note": "背离: 利好但风险收缩期，谨慎对待", "reason": "重大",
        }], ensure_ascii=False)
        monkeypatch.setattr(rtp, "_call_llm_api", lambda *a, **kw: raw)
        items = [{"title": "测试", "content": "内容", "published_at": ""}]
        out = rtp._llm_judge(items, env_context="风险收缩期")
        assert out[0]["env_note"].startswith("背离")

    def test_missing_env_note_defaults_empty(self, monkeypatch):
        raw = json.dumps([{
            "idx": 0, "title": "测试", "push": False, "score": 3,
            "direction": "neutral", "scope": "stock", "reason": "一般",
        }], ensure_ascii=False)
        monkeypatch.setattr(rtp, "_call_llm_api", lambda *a, **kw: raw)
        items = [{"title": "测试", "content": "内容", "published_at": ""}]
        out = rtp._llm_judge(items)
        assert out[0]["env_note"] == ""


class TestFormatPushAlertEnvNote:
    def test_env_note_line_shown(self):
        news = {"title": "测试标题", "content": "内容", "source": "测试", "published_at": ""}
        judge = {"direction": "bullish", "score": 8, "scope": "sector",
                 "sectors": ["AI算力"], "reason": "重大", "env_note": "背离: 利好但风险收缩期"}
        out = rtp.format_push_alert(news, judge)
        assert "**环境**: 背离: 利好但风险收缩期" in out

    def test_empty_env_note_omitted(self):
        news = {"title": "测试标题", "content": "内容", "source": "测试", "published_at": ""}
        judge = {"direction": "bullish", "score": 8, "scope": "sector",
                 "sectors": ["AI算力"], "reason": "重大", "env_note": ""}
        out = rtp.format_push_alert(news, judge)
        assert "**环境**" not in out


class TestFactorEnvLineFlows:
    def test_flows_part_appended(self):
        line = rtp._factor_env_line(_sample_snapshot())
        assert "主力净流出1940亿" in line

    def test_no_flows_key_old_snapshot_compatible(self):
        line = rtp._factor_env_line(_sample_snapshot(with_flows=False))
        assert "主力" not in line
        assert "市场环境" in line

    def test_zero_main_net_omitted(self):
        snap = _sample_snapshot()
        snap["flows"] = {"main_net_yi": 0.0}
        line = rtp._factor_env_line(snap)
        assert "主力" not in line


# ============================================================
# P2-3: signal_backtest
# ============================================================

def _klines(closes, start="2026-08-01"):
    """构造连续日K（start 起，跳过周末简化为连续日期）"""
    base = datetime.strptime(start, "%Y-%m-%d")
    return [{"date": (base + timedelta(days=i)).strftime("%Y-%m-%d"), "close": c}
            for i, c in enumerate(closes)]


class TestForwardReturns:
    def test_event_on_trading_day(self):
        kl = _klines([100, 110, 105, 120, 130])
        fr = sb._forward_returns(kl, "2026-08-01")
        assert fr["base"] == 100
        assert fr["ret_1"] == 10.0
        assert fr["ret_3"] == 20.0
        assert "ret_5" not in fr  # 数据不足

    def test_event_before_first_day(self):
        kl = _klines([100, 110])
        fr = sb._forward_returns(kl, "2026-07-30")
        assert fr["base"] == 100
        assert fr["ret_1"] == 10.0

    def test_event_after_last_day_no_data(self):
        kl = _klines([100, 110])
        assert sb._forward_returns(kl, "2026-09-01") == {}

    def test_last_day_no_forward(self):
        kl = _klines([100, 110])
        assert sb._forward_returns(kl, "2026-08-02") == {}


class TestBacktest:
    def _events(self):
        return [
            # 利多 + 上证后1日上涨 → hit
            {"dir": "bullish", "scope": "market", "stocks": [],
             "title_norm": "央行降准", "sectors": ["宏观"], "t": "2026-08-01 10:00:00"},
            # 利空 + 上证后1日上涨 → miss
            {"dir": "bearish", "scope": "market", "stocks": [],
             "title_norm": "地缘风险", "sectors": ["宏观"], "t": "2026-08-01 11:00:00"},
            # 个股级：stocks 命中 → 用个股K线（后1日上涨 → 利多 hit）
            {"dir": "mildly_bullish", "scope": "stock", "stocks": ["中际旭创"],
             "title_norm": "中际旭创获订单", "sectors": ["光模块"], "t": "2026-08-01 10:30:00"},
            # 未标注方向 → 跳过
            {"dir": "", "scope": "stock", "stocks": [],
             "title_norm": "无方向", "sectors": [], "t": "2026-08-01 12:00:00"},
        ]

    def test_backtest_aggregates(self, monkeypatch):
        monkeypatch.setattr(sb, "_resolve_symbol",
                            lambda name, cache: "sz300308" if name == "中际旭创" else "")
        monkeypatch.setattr(sb, "_fetch_kline", lambda symbol, lmt=120: (
            _klines([100, 105, 110, 115, 120, 125]) if symbol == "sh000001"
            else _klines([200, 210, 220, 230, 240, 250])))
        summary = sb.backtest(self._events(), days=30)
        assert summary["evaluated"] == 3
        assert summary["skipped"]["未标注方向"] == 1
        ov = summary["overall"]
        assert ov["n"] == 3
        assert ov["n_1"] == 3
        assert ov["hit_1"] == 2  # 降准 hit、地缘 miss、个股 hit
        # 分组
        assert summary["by_scope"]["market"]["n"] == 2
        assert summary["by_scope"]["stock"]["n"] == 1
        assert summary["by_group"]["利多"]["n"] == 2
        assert summary["by_group"]["利空"]["n"] == 1

    def test_sector_group_below_min_sample_excluded(self, monkeypatch):
        monkeypatch.setattr(sb, "_resolve_symbol", lambda name, cache: "")
        monkeypatch.setattr(sb, "_fetch_kline", lambda symbol, lmt=120: _klines([1, 2, 3, 4, 5, 6, 7]))
        summary = sb.backtest(self._events(), days=30)
        # 每板块样本 2 条 < 10 → by_sector 为空
        assert summary["by_sector"] == {}

    def test_report_renders(self, monkeypatch):
        monkeypatch.setattr(sb, "_resolve_symbol", lambda name, cache: "")
        monkeypatch.setattr(sb, "_fetch_kline", lambda symbol, lmt=120: _klines([100, 105, 110, 115, 120, 125]))
        summary = sb.backtest(self._events(), days=30)
        report = sb.build_report(summary)
        assert "信号质量回测报告" in report
        assert "总体一致率" in report
        assert "利多组" in report
        assert "明细" in report

    def test_report_cold_start_note(self, monkeypatch):
        """0 可评估样本时报告头部标注冷启动，防误读为回测结论"""
        monkeypatch.setattr(sb, "_resolve_symbol", lambda name, cache: "")
        monkeypatch.setattr(sb, "_fetch_kline", lambda symbol, lmt=120: _klines([1, 2, 3]))
        # 全部事件无 dir → evaluated=0
        events = [{"dir": "", "scope": "market", "stocks": [],
                   "title_norm": "无方向", "sectors": [], "t": "2026-08-01 10:00:00"}]
        summary = sb.backtest(events, days=30)
        assert summary["evaluated"] == 0
        report = sb.build_report(summary)
        assert "样本冷启动中" in report
        # 有可评估样本时不出现该标注
        summary2 = sb.backtest(self._events(), days=30)
        assert "样本冷启动中" not in sb.build_report(summary2)


class TestResolveSymbol:
    def test_exact_name_match(self, monkeypatch):
        text = 'v_hint="sz~300308~中际旭创~zjxc~GP-A";'
        monkeypatch.setattr(sb, "_http_get",
                            lambda url, params=None, encoding=None: text)
        assert sb._resolve_symbol("中际旭创", {}) == "sz300308"

    def test_fuzzy_name_rejected(self, monkeypatch):
        # 返回的名称与查询名不完全一致 → 不采用（防模糊误配）
        text = 'v_hint="sz~300999~中际集团~zjjt~GP-A";'
        monkeypatch.setattr(sb, "_http_get",
                            lambda url, params=None, encoding=None: text)
        assert sb._resolve_symbol("中际旭创", {}) == ""

    def test_cached(self, monkeypatch):
        monkeypatch.setattr(sb, "_http_get",
                            lambda url, params=None, encoding=None: 'v_hint="N";')
        cache = {"中际旭创": "sz300308"}
        assert sb._resolve_symbol("中际旭创", cache) == "sz300308"
