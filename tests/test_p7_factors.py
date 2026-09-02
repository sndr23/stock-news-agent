# filepath: tests/test_p7_factors.py
# -*- coding: utf-8 -*-
"""P7 单元测试（2026-08-19）：资金面 GC007 + 期权 PCR 影子因子

覆盖：
1. P7-1 fetch_liquidity：腾讯行情解析 gc007/gc001；空响应 → {}
2. P7-2 fetch_option_pcr：东财列表购/沽分桶统计 PCR；认购 0 → {}；部分页失败按已拉统计
3. 影子维度：流动性/期权情绪打分进 factors 但不改变综合分（与不传时一致）；
   shadow 集合只含非零影子维度；方向信号卡片"影子·未参与合成"标注
4. detect_liquidity_anomalies：GC007≥3.5% warning / 日内急升≥50% warning / 平稳无告警
5. detect_option_anomalies：PCR≥1.5 info 告警 / 正常区间无告警
6. build_snapshot / format_snapshot：liquidity/option 键与展示行
7. real_time_push：_factor_env_line 极端值上行、_llm_env_context 上下文、_snapshot_block 简报行
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import factor_collector as fc  # noqa: E402
import real_time_push as rtp  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单元测试：mock 数据源，无网络无推送


# ============================================================
# 辅助：方向分析环境（与 test_p5_gating 同口径）
# ============================================================
def _plain_env():
    """全中性环境：6 主维度全 0，综合分 0（影子维度影响判定的对照基线）"""
    tech = {"上证指数": {"available": True, "price": 3930.0, "change_pct": 0.0,
                     "trend": "震荡", "vol_ratio5": 1.0,
                     "breakout": False, "breakdown": False}}
    fx = {"fx_susdjpy": {"name": "美元/日元", "price": 156.0, "change_pct": 0.0}}
    return tech, fx, {}


# ============================================================
# P7-1 fetch_liquidity
# ============================================================
def _tencent_line(code: str, name: str, price: str, prev: str, chg_pct: str) -> str:
    """构造腾讯行情返回行：p[3]=现价 p[4]=昨收 p[32]=涨跌幅（其余占位）"""
    fields = ["1", name, code.replace("sh", ""), price, prev, price, "100"]
    fields += [""] * 25            # p[7]~p[31] 占位
    fields += [chg_pct, ""]        # p[32]=涨跌幅
    return f'v_{code}="' + "~".join(fields) + '";'


class TestFetchLiquidity:
    def test_parses_gc007_and_gc001(self, monkeypatch):
        raw = (_tencent_line("sh204007", "GC007", "1.425", "1.410", "1.06")
               + _tencent_line("sh204001", "GC001", "1.465", "1.390", "5.40"))
        monkeypatch.setattr(fc, "_http_get", lambda url, **kw: raw)
        out = fc.fetch_liquidity()
        assert out["gc007"] == {"price": 1.425, "change_pct": 1.06}
        assert out["gc001"]["price"] == 1.465

    def test_empty_response_returns_empty(self, monkeypatch):
        monkeypatch.setattr(fc, "_http_get", lambda url, **kw: "")
        assert fc.fetch_liquidity() == {}

    def test_malformed_fields_skipped(self, monkeypatch):
        raw = 'v_sh204007="1~GC007~204007~xx~1.410~1.415";'
        monkeypatch.setattr(fc, "_http_get", lambda url, **kw: raw)
        assert fc.fetch_liquidity() == {}


# ============================================================
# P7-2 fetch_option_pcr（2026-09-02 换源：东财名单 + 新浪 CON_OP_ 行情）
# ============================================================
def _opt_page(rows, total):
    return json.dumps({"data": {"diff": rows, "total": total}})


def _sina_lines(quotes):
    """构造新浪 CON_OP_ 响应体：quotes = {code: (vol, cp)}。

    列位（2026-09-02 实证）：[41]=成交量、[45]=C/P，响应至少 46 列。
    """
    lines = []
    for code, (vol, cp) in quotes.items():
        cols = ["x"] * 46
        cols[41] = str(vol)
        cols[45] = cp
        lines.append(f'var hq_str_CON_OP_{code}="{",".join(cols)}";')
    return "\n".join(lines)


class TestFetchOptionPcr:
    def _install(self, monkeypatch, roster_pages, sina_quotes):
        """roster_pages = {pn: (rows, total)}；sina_quotes = {code: (vol, cp)}。"""
        def fake_get(url, params=None, **kw):
            if "hq.sinajs.cn" in url:
                codes = [c.replace("CON_OP_", "") for c in url.split("list=CON_OP_")[1].split(",")]
                lines = []
                for c in codes:
                    if c not in sina_quotes:
                        continue
                    vol, cp = sina_quotes[c]
                    cols = ["x"] * 46
                    cols[41] = str(vol)
                    cols[45] = cp
                    lines.append(f'var hq_str_CON_OP_{c}="{",".join(cols)}";')
                return "\n".join(lines)
            if "push2.eastmoney.com" in url:
                rows, total = roster_pages[params.get("pn")]
                return _opt_page(rows, total)
            return ""

        monkeypatch.setattr(fc, "_http_get", fake_get)

    def test_pcr_from_call_put_volumes(self, monkeypatch):
        roster = [
            {"f12": "10005678", "f14": "50ETF购8月2900"},
            {"f12": "10005679", "f14": "50ETF沽8月2900"},
            {"f12": "10005680", "f14": "300ETF购8月4000"},
            {"f12": "10005681", "f14": "300ETF沽8月4000"},
        ]
        quotes = {"10005678": (1000, "C"), "10005679": (2000, "P"),
                  "10005680": (500, "C"), "10005681": (1500, "P")}
        self._install(monkeypatch, {1: (roster, 4)}, quotes)
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        out = fc.fetch_option_pcr()
        # PCR = (2000+1500) / (1000+500) = 2.333
        assert out["pcr"] == pytest.approx(2.333, abs=0.001)
        assert out["call_vol"] == 1500
        assert out["put_vol"] == 3500
        assert out["contracts"] == 4

    def test_no_call_volume_returns_empty(self, monkeypatch):
        roster = [{"f12": "1", "f14": "50ETF沽8月2900"}]
        self._install(monkeypatch, {1: (roster, 1)}, {"1": (100, "P")})
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        assert fc.fetch_option_pcr() == {}

    def test_nonfinite_or_negative_volume_is_not_counted(self, monkeypatch):
        """成交量非法 → 覆盖不足 → 整体放弃，不得污染 PCR。"""
        roster = [
            {"f12": "1", "f14": "50ETF购8月2900"},
            {"f12": "2", "f14": "50ETF沽8月2900"},
        ]
        self._install(monkeypatch, {1: (roster, 2)},
                      {"1": ("nan", "C"), "2": (-100, "P")})
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        assert fc.fetch_option_pcr() == {}

    def test_roster_pagination_stops_at_total(self, monkeypatch):
        page1 = [{"f12": str(i), "f14": "50ETF购8月2900"} for i in range(500)]
        page2 = [{"f12": "999", "f14": "50ETF沽8月2900"}]
        pages = {pn: (rows, 501) for pn, rows in ((1, page1), (2, page2))}
        calls = []

        def fake_get(url, params=None, **kw):
            if "push2.eastmoney.com" in url:
                calls.append(params.get("pn"))
                rows, total = pages[params.get("pn")]
                return _opt_page(rows, total)
            if "hq.sinajs.cn" in url:
                codes = [c.replace("CON_OP_", "") for c in url.split("list=CON_OP_")[1].split(",")]
                out = []
                for c in codes:
                    cp = "P" if c == "999" else "C"
                    cols = ["x"] * 46
                    cols[41] = "10" if c != "999" else "500"
                    cols[45] = cp
                    out.append(f'var hq_str_CON_OP_{c}="{",".join(cols)}";')
                return "\n".join(out)
            return ""

        monkeypatch.setattr(fc, "_http_get", fake_get)
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        out = fc.fetch_option_pcr()
        assert out["contracts"] == 501
        assert out["call_vol"] == 5000
        assert out["put_vol"] == 500
        assert calls == [1, 2]

    def test_failed_roster_page_aborts(self, monkeypatch):
        """名单页失败 → 名单不全 → 覆盖必然不足 → 宁缺毋假整体放弃。

        （旧东货行情源语义是"按已拉页统计"，换源后部分名单无行情可查，
        语义改为整体放弃；2026-09-02。）
        """
        page1 = [{"f12": "1", "f14": "50ETF购8月2900"},
                 {"f12": "2", "f14": "50ETF沽8月2900"}]

        def fake_get(url, params=None, **kw):
            if "push2.eastmoney.com" in url:
                if params.get("pn") == 1:
                    return _opt_page(page1, 300)
                return ""  # 第2页失败
            if "hq.sinajs.cn" in url:
                return _sina_lines({"1": (100, "C"), "2": (100, "P")})
            return ""

        monkeypatch.setattr(fc, "_http_get", fake_get)
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        assert fc.fetch_option_pcr() == {}

    def test_failed_roster_page_does_not_poison_same_day_cache(self, monkeypatch):
        """分页中断后的部分名单不得写入当日缓存，下一轮必须允许重拉。"""
        cache = {}
        page1 = [{"f12": "1", "f14": "50ETF购8月2900"}]

        def fake_get(url, params=None, **kw):
            if "eastmoney.com" in url:
                if params.get("pn") == 1:
                    return _opt_page(page1, 2)
                return ""
            return ""

        monkeypatch.setattr(fc, "_http_get", fake_get)
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)

        assert fc.fetch_option_pcr(roster_cache=cache) == {}
        assert cache == {}

    def test_sina_batch_failure_aborts_on_partial_coverage(self, monkeypatch):
        """新浪行情批失败 → 覆盖不足 → 整体放弃。"""
        roster = [{"f12": str(100 + i), "f14": "50ETF购8月2900"} for i in range(120)]
        quotes = {str(100 + i): (10, "C") for i in range(60)}  # 仅首批有效

        def fake_get(url, params=None, **kw):
            if "push2.eastmoney.com" in url:
                return _opt_page(roster, 120)
            if "hq.sinajs.cn" in url:
                return _sina_lines({c: q for c, q in quotes.items()
                                    if f"CON_OP_{c}," in url or url.endswith(f"CON_OP_{c}")})
            return ""

        monkeypatch.setattr(fc, "_http_get", fake_get)
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        assert fc.fetch_option_pcr() == {}

    def test_cp_fallback_by_name_when_flag_invalid(self, monkeypatch):
        """[45] C/P 缺失时按名称"购/沽"兜底分类。"""
        roster = [{"f12": "1", "f14": "50ETF购8月2900"},
                  {"f12": "2", "f14": "50ETF沽8月2900"}]
        self._install(monkeypatch, {1: (roster, 2)},
                      {"1": (100, ""), "2": (100, "")})
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        out = fc.fetch_option_pcr()
        assert out["pcr"] == 1.0
        assert out["call_vol"] == 100

    def test_no_trade_dash_counts_as_zero_and_keeps_coverage(self, monkeypatch):
        """[41]='-'（当日无成交）按 0 计入覆盖，对齐旧东财 f5=0 口径。

        远月虚值合约常态无成交；若误判缺失 → 覆盖不足 → 整体拒绝。
        """
        roster = [{"f12": "1", "f14": "50ETF购8月2900"},
                  {"f12": "2", "f14": "50ETF沽8月2900"},
                  {"f12": "3", "f14": "50ETF购12月3000"}]
        self._install(monkeypatch, {1: (roster, 3)},
                      {"1": (300, "C"), "2": (100, "P"), "3": ("-", "C")})
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        out = fc.fetch_option_pcr()
        assert out["contracts"] == 3  # 无成交合约计入覆盖
        assert out["call_vol"] == 300  # 无成交合约按 0 计，对比值无贡献
        assert out["pcr"] == pytest.approx(100 / 300, abs=0.001)

    def test_missing_quote_row_rejects_coverage(self, monkeypatch):
        """响应行缺失（名单有、行情无）= 数据缺失 → 覆盖不足 → 整体拒绝。"""
        roster = [{"f12": "1", "f14": "50ETF购8月2900"},
                  {"f12": "2", "f14": "50ETF沽8月2900"}]
        self._install(monkeypatch, {1: (roster, 2)}, {"1": (100, "C")})  # 2 无行情行
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        assert fc.fetch_option_pcr() == {}

    def test_today_roster_cache_skips_em_request(self, monkeypatch):
        """当日名单缓存命中：不再请求东财名单，直接走行情。"""
        cache = {"date": fc.datetime.now(fc.BJT).date().isoformat(),
                 "total": 2, "roster": [["1", "50ETF购8月2900"], ["2", "50ETF沽8月2900"]]}
        calls = []

        def fake_get(url, params=None, **kw):
            if "push2.eastmoney.com" in url:
                calls.append("em")  # 命中缓存时不应发生
                return ""
            if "hq.sinajs.cn" in url:
                codes = [c.replace("CON_OP_", "") for c in url.split("list=CON_OP_")[1].split(",")]
                q = {"1": (300, "C"), "2": (100, "P")}
                lines = []
                for c in codes:
                    if c not in q:
                        continue
                    cols = ["x"] * 46
                    cols[41] = str(q[c][0])
                    cols[45] = q[c][1]
                    lines.append(f'var hq_str_CON_OP_{c}="{",".join(cols)}";')
                return "\n".join(lines)
            return ""

        monkeypatch.setattr(fc, "_http_get", fake_get)
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        out = fc.fetch_option_pcr(roster_cache=cache)
        assert out["contracts"] == 2
        assert out["pcr"] == pytest.approx(100 / 300, abs=0.001)
        assert calls == []  # 未请求东财名单

    def test_yesterday_roster_cache_is_refetched(self, monkeypatch):
        """跨日缓存失效：必须重新拉名单（合约挂牌/到期按日变）。"""
        stale = {"date": "2020-01-01", "total": 9, "roster": [["z", "旧合约"]]}
        roster = [{"f12": "1", "f14": "50ETF购8月2900"},
                  {"f12": "2", "f14": "50ETF沽8月2900"}]
        self._install(monkeypatch, {1: (roster, 2)}, {"1": (300, "C"), "2": (100, "P")})
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        out = fc.fetch_option_pcr(roster_cache=stale)
        assert out["contracts"] == 2
        assert stale["date"] != "2020-01-01"  # 成功拉取后原地回写当日名单

    def test_primary_host_empty_data_falls_back_to_delay_mirror(self, monkeypatch):
        """主域返回 HTTP 200 但 data 为空时必须降级镜像，不得误判命中。

        2026-09-02 云端实证：主域对 fs=m:10 返回空 data（响应非空），
        原 `if text: break` 直接采纳主域空结果 → roster=0 → 名单失败。
        """
        roster = [{"f12": "1", "f14": "50ETF购8月2900"},
                  {"f12": "2", "f14": "50ETF沽8月2900"}]

        def fake_get(url, params=None, **kw):
            if "push2delay.eastmoney.com" in url:
                return _opt_page(roster, 2)  # 镜像有数据
            if "push2.eastmoney.com" in url:
                return json.dumps({"data": None})  # 主域空 data
            if "hq.sinajs.cn" in url:
                return _sina_lines({"1": (300, "C"), "2": (100, "P")})
            return ""

        monkeypatch.setattr(fc, "_http_get", fake_get)
        monkeypatch.setattr(fc.time, "sleep", lambda s: None)
        out = fc.fetch_option_pcr()
        assert out["contracts"] == 2
        assert out["pcr"] == pytest.approx(100 / 300, abs=0.001)


# ============================================================
# 影子维度：打分记录但不影响综合分
# ============================================================
class TestShadowDims:
    def test_liquidity_tight_scores_negative_without_score_change(self):
        tech, fx, hist = _plain_env()
        base = fc._direction_analysis(tech, {}, fx, "neutral", hist)
        tight = fc._direction_analysis(tech, {}, fx, "neutral", hist,
                                       liquidity={"gc007": {"price": 3.5, "change_pct": 20}})
        # 影子维度不改变综合分与方向
        assert tight["score"] == base["score"]
        assert tight["direction"] == base["direction"]
        # 流动性影子维度 -1，且 shadow 集合含"流动性"
        fac = {name: s for name, s, _ in tight["factors"]}
        assert fac["流动性"] == -1.0
        assert "流动性" in tight["shadow"]
        # 影子维度恒定追加（liquidity=None 时也记 0 分，序列完整供 IC 回测）
        assert len(tight["factors"]) == len(base["factors"])
        assert "流动性" in fac and "期权情绪" in fac
        # 生效权重不含影子维度
        assert "流动性" not in tight["eff_weights"]

    def test_gc007_spike_scores_negative(self):
        tech, fx, hist = _plain_env()
        a = fc._direction_analysis(tech, {}, fx, "neutral", hist,
                                   liquidity={"gc007": {"price": 2.6, "change_pct": 45}})
        fac = {name: s for name, s, _ in a["factors"]}
        assert fac["流动性"] == -1.0

    def test_gc007_calm_scores_zero(self):
        tech, fx, hist = _plain_env()
        a = fc._direction_analysis(tech, {}, fx, "neutral", hist,
                                   liquidity={"gc007": {"price": 1.425, "change_pct": 1.0}})
        fac = {name: s for name, s, _ in a["factors"]}
        assert fac["流动性"] == 0.0
        assert "流动性" not in a["shadow"]

    def test_option_pcr_panic_and_greed(self):
        tech, fx, hist = _plain_env()
        panic = fc._direction_analysis(tech, {}, fx, "neutral", hist,
                                       option={"pcr": 1.4})
        fac_p = {name: s for name, s, _ in panic["factors"]}
        assert fac_p["期权情绪"] == -1.0
        assert "期权情绪" in panic["shadow"]

        greed = fc._direction_analysis(tech, {}, fx, "neutral", hist,
                                       option={"pcr": 0.5})
        fac_g = {name: s for name, s, _ in greed["factors"]}
        assert fac_g["期权情绪"] == 1.0

        calm = fc._direction_analysis(tech, {}, fx, "neutral", hist,
                                      option={"pcr": 0.85})
        fac_c = {name: s for name, s, _ in calm["factors"]}
        assert fac_c["期权情绪"] == 0.0
        assert "期权情绪" not in calm["shadow"]

    def test_shadow_not_gated_by_high_vol(self):
        """高波门控只升权主维度利空维度，影子维度不进 eff_weights（无门控副作用）"""
        tech, fx, hist = _plain_env()
        vol = {"上证指数": {"regime": "高波", "vol20": 28.5, "pctile": 85}}
        a = fc._direction_analysis(tech, {}, fx, "neutral", hist, vol=vol,
                                   liquidity={"gc007": {"price": 3.5, "change_pct": 0}})
        assert "流动性" not in a["eff_weights"]
        assert a["eff_weights"]["波动率"] == 1.5  # 主维度门控不受影响


# ============================================================
# 异动检测
# ============================================================
class TestDetectLiquidityAnomalies:
    def test_high_level_warns(self):
        sigs = fc.detect_liquidity_anomalies(
            {"gc007": {"price": 3.6, "change_pct": 10}})
        assert len(sigs) == 1
        assert sigs[0]["level"] == "warning"
        assert sigs[0]["key"] == "liquidity_gc007_high"

    def test_spike_warns(self):
        sigs = fc.detect_liquidity_anomalies(
            {"gc007": {"price": 2.8, "change_pct": 60}})
        assert len(sigs) == 1
        assert sigs[0]["key"] == "liquidity_gc007_spike"
        assert sigs[0]["level"] == "warning"

    def test_calm_no_signal(self):
        assert fc.detect_liquidity_anomalies(
            {"gc007": {"price": 1.425, "change_pct": 1.06}}) == []
        assert fc.detect_liquidity_anomalies({}) == []

    def test_risk_off_includes_liquidity_warning(self):
        """流动性 warning 并入 risk_off 口径（calc_risk_state 任一 warning 即 risk_off）"""
        sigs = fc.detect_liquidity_anomalies({"gc007": {"price": 4.0, "change_pct": 0}})
        assert fc.calc_risk_state(sigs) == "risk_off"


class TestDetectOptionAnomalies:
    def test_pcr_extreme_info_signal(self):
        sigs = fc.detect_option_anomalies({"pcr": 1.6, "call_vol": 100, "put_vol": 160})
        assert len(sigs) == 1
        assert sigs[0]["level"] == "info"  # 情绪指标不切 risk_off
        assert sigs[0]["key"] == "option_pcr_panic"

    def test_pcr_normal_no_signal(self):
        assert fc.detect_option_anomalies({"pcr": 0.9, "call_vol": 100, "put_vol": 90}) == []
        assert fc.detect_option_anomalies({}) == []

    def test_option_info_keeps_neutral_risk(self):
        sigs = fc.detect_option_anomalies({"pcr": 2.0, "call_vol": 100, "put_vol": 200})
        assert fc.calc_risk_state(sigs) == "neutral"


# ============================================================
# 快照与展示
# ============================================================
class TestSnapshotAndDisplay:
    def test_build_snapshot_liquidity_option_keys(self):
        snap = fc.build_snapshot({}, {}, {}, "neutral",
                                 liquidity={"gc007": {"price": 1.425, "change_pct": 1.06},
                                            "gc001": {"price": 1.465, "change_pct": 5.4}},
                                 option={"pcr": 0.85, "call_vol": 1000, "put_vol": 850})
        assert snap["liquidity"]["gc007"]["price"] == 1.425
        assert snap["option"]["pcr"] == 0.85

    def test_build_snapshot_omits_empty_keys(self):
        snap = fc.build_snapshot({}, {}, {}, "neutral")
        assert "liquidity" not in snap
        assert "option" not in snap

    def test_format_snapshot_lines(self):
        text = fc.format_snapshot({}, {}, {}, liquidity={"gc007": {"price": 3.6,
                                                                  "change_pct": 20}},
                                  option={"pcr": 1.4, "call_vol": 100, "put_vol": 140})
        assert "GC007 3.60%" in text
        assert "资金面收紧" in text
        assert "期权 PCR 1.40" in text

    def test_format_direction_signal_shadow_tag(self):
        tech, fx, hist = _plain_env()
        a = fc._direction_analysis(tech, {}, fx, "neutral", hist,
                                   liquidity={"gc007": {"price": 3.5, "change_pct": 0}})
        text = fc.format_direction_signal(a, "中性")
        assert "影子·未参与合成" in text
        assert "流动性" in text


# ============================================================
# real_time_push 展示层
# ============================================================
def _snap_with(extra: dict, ts=None) -> dict:
    """构造带 ts 的快照（ts 默认当前时刻，确保不过期）"""
    base = {"ts": ts or datetime.now().strftime("%Y-%m-%d %H:%M"),
            "risk_state": "neutral"}
    base.update(extra)
    return base


class TestRealtimePushDisplay:
    def test_env_line_gc007_extreme(self):
        line = rtp._factor_env_line(_snap_with({
            "liquidity": {"gc007": {"price": 3.2, "change_pct": 10}}}))
        assert "⚠️GC007 3.20%" in line

    def test_env_line_pcr_extreme(self):
        line = rtp._factor_env_line(_snap_with({
            "option": {"pcr": 1.55, "call_vol": 100, "put_vol": 155}}))
        assert "⚠️PCR 1.55(恐慌)" in line  # P10：PCR 始终显示 + 情绪标注

    def test_env_line_calm_values_shown(self):
        """P10：平静的 GC007/PCR 也始终显示（不带⚠），风险档位为中性"""
        line = rtp._factor_env_line(_snap_with({
            "liquidity": {"gc007": {"price": 1.425, "change_pct": 1.0}},
            "option": {"pcr": 0.85}}))
        assert "GC007 1.43%" in line
        assert "PCR 0.85(中性)" in line
        assert "🟢中性" in line

    def test_llm_env_context_liquidity_and_option(self):
        ctx = rtp._llm_env_context(_snap_with({
            "liquidity": {"gc007": {"price": 3.4, "change_pct": 10}},
            "option": {"pcr": 1.4}}))
        assert "GC007资金面利率3.40%" in ctx
        assert "资金面收紧" in ctx
        assert "期权PCR1.40" in ctx
        assert "恐慌对冲占优" in ctx

    def test_snapshot_block_brief_lines(self):
        lines = rtp._snapshot_block({"snapshot": _snap_with({
            "liquidity": {"gc007": {"price": 1.425, "change_pct": 1.06},
                          "gc001": {"price": 1.465}},
            "option": {"pcr": 0.85, "call_vol": 1000, "put_vol": 850}})},
            "因子环境")
        text = "\n".join(lines)
        assert "资金面利率: GC007 1.43%｜GC001 1.47%（平稳）" in text
        assert "期权情绪: PCR 0.85（情绪中性）" in text
