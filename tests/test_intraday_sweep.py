# filepath: tests/test_intraday_sweep.py
"""SNA-06 盘中异动反查链路单测（2026-09-07）

覆盖: 分钟K线解析/异动检测/阈值窗口、关联新闻反查、冷却去重、
无消息推送/有消息压制、非交易时段跳过、状态裁剪、推送格式(红涨绿跌)。
全部 mock 网络层，遵循项目 pytest -m unit 门禁。
"""
import json
import pytest
from datetime import datetime, timedelta, timezone

pytestmark = pytest.mark.unit

from scripts.intraday_sweep import (
    BJT, SWEEP_MOVE_PCT, SWEEP_NEWS_LOOKUP_MIN,
    detect_sweeps, find_related_news, filter_by_cooldown,
    format_sweep_alert, run_sweep, _tencent_symbol, _parse_mkline_time,
)

NOW = datetime(2026, 9, 7, 14, 30, tzinfo=BJT)
WATCH = [{"code": "300308", "name": "中际旭创"}, {"code": "688498", "name": "源杰科技"}]


class _Resp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


def _mkline_session(sym_rows: dict):
    """mock requests.get：sym_rows = {symbol: [[time, o, c, h, l, v, {}], ...]}"""
    def get(url, headers=None, timeout=None):
        for sym, rows in sym_rows.items():
            if sym in url:
                return _Resp(json.dumps({"data": {sym: {"m1": rows}}}))
        return _Resp("{}")
    return get


def _bars(times_closes):
    """[(time_str, close)] → mkline rows（开/高/低随便填，只用 close）"""
    return [[t, str(c), str(c), str(c), str(c), "100", {}] for t, c in times_closes]


class TestTencentSymbol:
    def test_prefix(self):
        assert _tencent_symbol("300308") == "sz300308"
        assert _tencent_symbol("688498") == "sh688498"
        assert _tencent_symbol("600183") == "sh600183"
        assert _tencent_symbol("000636") == "sz000636"

    def test_invalid(self):
        assert _tencent_symbol("") == ""
        assert _tencent_symbol("abc") == ""
        assert _tencent_symbol("12345") == ""


class TestParseMklineTime:
    def test_full(self):
        dt = _parse_mkline_time("202609071451", "2026-09-07")
        assert dt == datetime(2026, 9, 7, 14, 51, tzinfo=BJT)

    def test_bad(self):
        assert _parse_mkline_time("garbage", "2026-09-07") is None
        assert _parse_mkline_time("", "2026-09-07") is None


class TestDetectSweeps:
    def test_spike_detected(self):
        # 14:20 有一根 +3% 的急拉
        rows = _bars([("202609071418", 880.0), ("202609071419", 881.0),
                      ("202609071420", 907.4), ("202609071421", 908.0)])
        get = _mkline_session({"sz300308": rows})
        sweeps = detect_sweeps(get, WATCH[:1], now=NOW)
        assert len(sweeps) == 1
        s = sweeps[0]
        assert s["code"] == "300308" and s["name"] == "中际旭创"
        assert abs(s["move_pct"] - 3.0) < 0.3
        assert s["time"] == datetime(2026, 9, 7, 14, 20, tzinfo=BJT)

    def test_below_threshold_not_detected(self):
        rows = _bars([("202609071418", 880.0), ("202609071419", 885.0)])  # +0.57%
        get = _mkline_session({"sz300308": rows})
        assert detect_sweeps(get, WATCH[:1], now=NOW) == []

    def test_drop_detected(self):
        rows = _bars([("202609071418", 880.0), ("202609071419", 853.0)])  # -3.07%
        get = _mkline_session({"sz300308": rows})
        sweeps = detect_sweeps(get, WATCH[:1], now=NOW)
        assert len(sweeps) == 1 and sweeps[0]["move_pct"] < 0

    def test_outside_window_ignored(self):
        # 异动发生在 60 分钟前，超出默认 30 分钟窗口
        old_t = (NOW - timedelta(minutes=60)).strftime("%Y%m%d%H%M")
        rows = _bars([(old_t, 880.0), ("202609071421", 908.0)])
        # 注意 old_t 是 13:30 那根的"当前分钟"，涨跌发生在 13:30→下一根之间，
        # 但下一根时间戳不在 rows 里——构造连续两根都过期的数据
        rows = _bars([(old_t, 880.0),
                      ((NOW - timedelta(minutes=59)).strftime("%Y%m%d%H%M"), 908.0)])
        get = _mkline_session({"sz300308": rows})
        assert detect_sweeps(get, WATCH[:1], now=NOW) == []

    def test_keeps_max_move_in_window(self):
        rows = _bars([("202609071418", 880.0), ("202609071419", 907.0),   # +3.07%
                      ("202609071420", 880.0), ("202609071421", 898.5)])  # +2.1%
        get = _mkline_session({"sz300308": rows})
        sweeps = detect_sweeps(get, WATCH[:1], now=NOW)
        assert len(sweeps) == 1
        assert sweeps[0]["time"].strftime("%H:%M") == "14:19"  # 保留最大幅度那次

    def test_network_fail_returns_empty(self):
        def get(url, headers=None, timeout=None):
            raise ConnectionError("boom")
        assert detect_sweeps(get, WATCH[:1], now=NOW) == []

    def test_bad_rows_skipped(self):
        rows = [["bad"], ["202609071418", "x", "y", "z", "w", "1", {}],
                ["202609071419", "880", "880", "880", "880", "100", {}],
                ["202609071420", "907", "907", "907", "907", "100", {}]]
        get = _mkline_session({"sz300308": rows})
        sweeps = detect_sweeps(get, WATCH[:1], now=NOW)
        assert len(sweeps) == 1  # 坏行跳过，好行保留

    def test_multiple_stocks(self):
        r1 = _bars([("202609071418", 880.0), ("202609071419", 907.0)])
        r2 = _bars([("202609071418", 1600.0), ("202609071419", 1640.0)])  # +2.5%
        sym_rows = {"sz300308": r1, "sh688498": r2}
        get = _mkline_session(sym_rows)
        sweeps = detect_sweeps(get, WATCH, now=NOW)
        assert len(sweeps) == 2


class TestFindRelatedNews:
    def _sweep(self, t=None):
        return {"code": "300308", "name": "中际旭创",
                "time": t or datetime(2026, 9, 7, 14, 20, tzinfo=BJT),
                "move_pct": 3.0, "price": 907.0}

    def test_hit_in_window(self):
        news = [{"title": "中际旭创获海外大单", "source": "财联社电报",
                 "published_at": "2026-09-07 14:25:00"}]
        hits = find_related_news(self._sweep(), news)
        assert len(hits) == 1 and "大单" in hits[0]["title"]

    def test_miss_outside_window(self):
        news = [{"title": "中际旭创获海外大单", "source": "财联社电报",
                 "published_at": "2026-09-07 13:00:00"}]  # 早于异动 80 分钟
        assert find_related_news(self._sweep(), news) == []

    def test_no_timestamp_treated_as_related(self):
        # 无时间戳 → 保守视为关联（宁可不推小作文，不误报）
        news = [{"title": "中际旭创龙虎榜数据", "source": "同花顺", "published_at": ""}]
        assert len(find_related_news(self._sweep(), news)) == 1

    def test_unrelated_title_ignored(self):
        news = [{"title": "新易盛订单传闻", "source": "财联社",
                 "published_at": "2026-09-07 14:22:00"}]
        assert find_related_news(self._sweep(), news) == []

    def test_code_match(self):
        news = [{"title": "300308盘中放量", "source": "东方财富",
                 "published_at": "2026-09-07 14:21:00"}]
        assert len(find_related_news(self._sweep(), news)) == 1


class TestCooldown:
    def test_same_direction_blocked(self):
        sweeps = [{"code": "300308", "move_pct": 3.0,
                   "time": datetime(2026, 9, 7, 14, 20, tzinfo=BJT)}]
        state = [{"key": "300308#up", "t": "2026-09-07 14:10:00"}]
        assert filter_by_cooldown(sweeps, state, NOW) == []

    def test_expired_cooldown_passes(self):
        sweeps = [{"code": "300308", "move_pct": 3.0,
                   "time": datetime(2026, 9, 7, 14, 20, tzinfo=BJT)}]
        state = [{"key": "300308#up", "t": "2026-09-07 12:00:00"}]  # >1h 前
        assert len(filter_by_cooldown(sweeps, state, NOW)) == 1

    def test_opposite_direction_passes(self):
        sweeps = [{"code": "300308", "move_pct": -3.0,
                   "time": datetime(2026, 9, 7, 14, 20, tzinfo=BJT)}]
        state = [{"key": "300308#up", "t": "2026-09-07 14:10:00"}]  # 上一次是急拉
        out = filter_by_cooldown(sweeps, state, NOW)
        assert len(out) == 1


class TestRunSweep:
    def test_no_news_pushes_xiaozuo(self):
        """异动 + 反查无消息 → 推送（用户拍板口径）。"""
        rows = _bars([("202609071418", 880.0), ("202609071419", 907.0)])
        get = _mkline_session({"sz300308": rows})
        pushed = []
        state = []
        stats = run_sweep(get, WATCH[:1], [], state, now=NOW,
                          send_push=lambda t, c: (pushed.append((t, c)) or {"code": 200}))
        assert stats["detected"] == 1 and stats["pushed"] == 1
        assert stats["suppressed_with_news"] == 0
        assert len(pushed) == 1
        assert "疑似小作文" in pushed[0][1]
        assert "中际旭创" in pushed[0][0]
        assert state and state[0]["key"] == "300308#up"

    def test_with_news_suppresses_push(self):
        """异动 + 找到关联消息 → 不推（30 分钟正规推送覆盖）。"""
        rows = _bars([("202609071418", 880.0), ("202609071419", 907.0)])
        get = _mkline_session({"sz300308": rows})
        news = [{"title": "中际旭创获大额订单", "source": "财联社电报",
                 "published_at": "2026-09-07 14:22:00"}]
        state = []
        stats = run_sweep(get, WATCH[:1], news, state, now=NOW,
                          send_push=lambda t, c: {"code": 200})
        assert stats["detected"] == 1 and stats["pushed"] == 0
        assert stats["suppressed_with_news"] == 1
        # 冷却仍登记（防同股反复进候选）
        assert state and state[0]["related_news"] == 1

    def test_cooldown_blocks_second_round(self):
        rows = _bars([("202609071418", 880.0), ("202609071419", 907.0)])
        get = _mkline_session({"sz300308": rows})
        state = []
        run_sweep(get, WATCH[:1], [], state, now=NOW,
                  send_push=lambda t, c: {"code": 200})
        # 同一轮内第二次扫描 → 冷却拦截
        stats2 = run_sweep(get, WATCH[:1], [], state, now=NOW,
                           send_push=lambda t, c: {"code": 200})
        assert stats2["detected"] == 0

    def test_off_hours_skipped(self):
        # 周六
        sat = datetime(2026, 9, 5, 14, 0, tzinfo=BJT)
        stats = run_sweep(_mkline_session({}), WATCH[:1], [], [],
                          now=sat, send_push=lambda t, c: {"code": 200})
        assert stats == {"detected": 0, "pushed": 0, "suppressed_with_news": 0}
        # 盘前过早
        early = datetime(2026, 9, 7, 9, 10, tzinfo=BJT)
        stats2 = run_sweep(_mkline_session({}), WATCH[:1], [], [],
                           now=early, send_push=lambda t, c: {"code": 200})
        assert stats2["detected"] == 0

    def test_state_trimmed_48h(self):
        rows = _bars([("202609071418", 880.0), ("202609071419", 907.0)])
        get = _mkline_session({"sz300308": rows})
        state = [{"key": "300308#up", "t": "2026-09-04 10:00:00"},
                 {"key": "300308#up", "t": "2026-09-07 13:00:00"}]
        run_sweep(get, WATCH[:1], [], state, now=NOW,
                  send_push=lambda t, c: {"code": 200})
        ts = [e["t"] for e in state]
        assert all(t >= "2026-09-05 14:30" for t in ts)
        assert "2026-09-04 10:00:00" not in ts

    def test_dry_run_no_push(self):
        rows = _bars([("202609071418", 880.0), ("202609071419", 907.0)])
        get = _mkline_session({"sz300308": rows})
        calls = []
        stats = run_sweep(get, WATCH[:1], [], [], now=NOW, dry_run=True,
                          send_push=lambda t, c: (calls.append(1) or {"code": 200}))
        assert stats["pushed"] == 1 and calls == []  # dry-run 只记日志

    def test_push_fail_not_counted(self):
        rows = _bars([("202609071418", 880.0), ("202609071419", 907.0)])
        get = _mkline_session({"sz300308": rows})
        stats = run_sweep(get, WATCH[:1], [], [], now=NOW,
                          send_push=lambda t, c: {"code": 500})
        assert stats["detected"] == 1 and stats["pushed"] == 0

    def test_max_push_per_round(self):
        """4 只同时异动 → 只推前 3 条（SWEEP_MAX_PUSH_PER_ROUND 默认 3）。"""
        watch4 = [{"code": f"30030{i}", "name": f"股{i}"} for i in range(4)]
        sym_rows = {}
        for s in watch4:
            sym = _tencent_symbol(s["code"])
            sym_rows[sym] = _bars([("202609071418", 100.0), ("202609071419", 103.0)])
        get = _mkline_session(sym_rows)
        stats = run_sweep(get, watch4, [], [], now=NOW,
                          send_push=lambda t, c: {"code": 200})
        assert stats["detected"] == 4 and stats["pushed"] == 3


class TestFormatAlert:
    def test_up_uses_red(self):
        t, c = format_sweep_alert({"name": "中际旭创", "code": "300308",
                                   "move_pct": 3.0, "price": 907.0,
                                   "time": datetime(2026, 9, 7, 14, 20, tzinfo=BJT)})
        assert "🔴" in c and "急拉" in t
        assert "🟢" not in c

    def test_down_uses_green(self):
        t, c = format_sweep_alert({"name": "中际旭创", "code": "300308",
                                   "move_pct": -3.0, "price": 853.0,
                                   "time": datetime(2026, 9, 7, 14, 20, tzinfo=BJT)})
        assert "🟢" in c and "急跌" in t
        assert "🔴" not in c


class TestEnvThresholds:
    def test_custom_threshold(self):
        rows = _bars([("202609071418", 880.0), ("202609071419", 893.0)])  # +1.48%
        get = _mkline_session({"sz300308": rows})
        assert detect_sweeps(get, WATCH[:1], now=NOW) == []  # 默认2%不触发
        sweeps = detect_sweeps(get, WATCH[:1], now=NOW, threshold_pct=1.0)
        assert len(sweeps) == 1  # 阈值降到1%触发
