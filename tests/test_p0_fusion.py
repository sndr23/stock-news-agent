# filepath: tests/test_p0_fusion.py
"""P0 融合展示单元测试（2026-08-19）

覆盖：
1. factor_collector.build_snapshot：紧凑快照构建与字段防御
2. real_time_push._factor_env_line：市场环境行生成（缺失/过期/未来时间退化）
3. format_push_alert：factor_env 参数向后兼容
4. _snapshot_block：盘前/盘后简报的因子环境块
5. run_morning_brief / run_evening_review：dry-run 端到端（mock 数据源，无网络无推送）
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
    """构造一份完整因子快照（ts 默认当前时间，保证不过期）"""
    return {
        "ts": ts or datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
        "risk_state": "risk_off",
        "indexes": {
            "上证指数": {"price": 3913.93, "change_pct": -0.50, "trend": "多头排列"},
            "创业板指": {"price": 3600.35, "change_pct": 0.45, "trend": "均线纠缠"},
        },
        "basis": {
            "IC": {"basis_pct": -0.52, "annual_pct": -8.1},
            "IM": {"basis_pct": -0.83, "annual_pct": -12.6},
        },
        "fx": {
            "美元/日元": {"price": 154.20, "change_pct": 0.83},
            "美元/在岸人民币": {"price": 7.1234, "change_pct": -0.05},
        },
    }


# ============================================================
# factor_collector.build_snapshot
# ============================================================

class TestBuildSnapshot:
    def test_compact_fields(self):
        tech = {
            "上证指数": {"available": True, "price": 3913.93, "change_pct": -0.50,
                       "trend": "多头排列"},
        }
        basis = {"IC": {"basis_pct": -0.52, "annual_pct": -8.1}}
        fx = {"fx_susdjpy": {"price": 154.20, "change_pct": 0.83}}
        snap = fc.build_snapshot(tech, basis, fx, "risk_off")

        assert snap["risk_state"] == "risk_off"
        assert snap["ts"]
        assert snap["indexes"]["上证指数"]["change_pct"] == -0.5
        assert snap["basis"]["IC"]["basis_pct"] == -0.52
        assert snap["fx"]["美元/日元"]["price"] == 154.2

    def test_unavailable_index_skipped(self):
        tech = {"上证指数": {"available": False}, "创业板指": {"available": True, "price": 3600.0,
                                                             "change_pct": 0.4, "trend": ""}}
        snap = fc.build_snapshot(tech, {}, {}, "neutral")
        assert "上证指数" not in snap["indexes"]
        assert "创业板指" in snap["indexes"]

    def test_empty_inputs_tolerated(self):
        snap = fc.build_snapshot({}, {}, {}, "neutral")
        assert snap["indexes"] == {} and snap["basis"] == {} and snap["fx"] == {}
        assert snap["risk_state"] == "neutral"


# ============================================================
# real_time_push._factor_env_line
# ============================================================

class TestFactorEnvLine:
    def test_full_snapshot(self):
        line = rtp._factor_env_line(_sample_snapshot())
        assert "市场环境" in line
        assert "⚠️风险收缩期" in line
        assert "IC -0.52%" in line and "IM -0.83%" in line
        assert "美元/日元 +0.83%" in line
        assert "上证 ▼-0.50%" in line

    def test_neutral_risk_omits_warning(self):
        snap = _sample_snapshot()
        snap["risk_state"] = "neutral"
        line = rtp._factor_env_line(snap)
        assert "风险收缩" not in line

    def test_missing_or_empty_returns_empty(self):
        assert rtp._factor_env_line(None) == ""
        assert rtp._factor_env_line({}) == ""

    def test_stale_snapshot_returns_empty(self):
        stale = datetime.now(BJT) - timedelta(hours=49)
        assert rtp._factor_env_line(_sample_snapshot(ts=stale.strftime("%Y-%m-%d %H:%M"))) == ""

    def test_future_snapshot_returns_empty(self):
        future = datetime.now(BJT) + timedelta(hours=2)
        assert rtp._factor_env_line(_sample_snapshot(ts=future.strftime("%Y-%m-%d %H:%M"))) == ""

    def test_bad_ts_returns_empty(self):
        snap = _sample_snapshot()
        snap["ts"] = "not-a-date"
        assert rtp._factor_env_line(snap) == ""

    def test_no_data_fields_returns_empty(self):
        snap = {"ts": datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
                "risk_state": "neutral", "indexes": {}, "basis": {}, "fx": {}}
        assert rtp._factor_env_line(snap) == ""


# ============================================================
# format_push_alert factor_env 参数
# ============================================================

class TestFormatPushAlertEnv:
    def test_env_line_included(self):
        out = rtp.format_push_alert(
            {"title": "日本央行加息", "content": "", "source": "华尔街见闻"},
            {"direction": "bearish", "score": 8, "scope": "market"},
            factor_env="**市场环境**(08-19 13:45): ⚠️风险收缩期 | 上证 ▼-0.50%")
        assert "市场环境" in out
        assert "风险收缩期" in out

    def test_default_no_env_line(self):
        out = rtp.format_push_alert(
            {"title": "T", "content": "", "source": "S"},
            {"direction": "neutral"})
        assert "市场环境" not in out


# ============================================================
# _snapshot_block
# ============================================================

class TestSnapshotBlock:
    def test_missing_snapshot_message(self):
        lines = rtp._snapshot_block({}, "因子环境")
        assert any("快照缺失" in ln for ln in lines)

    def test_full_block(self):
        factor_state = {"snapshot": _sample_snapshot(), "last_direction": "偏空"}
        lines = rtp._snapshot_block(factor_state, "收盘因子环境")
        text = "\n".join(lines)
        assert "风险收缩期" in text
        assert "上证指数" in text and "创业板指" in text
        assert "IC 贴水0.52%" in text
        assert "美元/日元" in text
        assert "偏空" in text


# ============================================================
# factor_collector 联动增强：_recent_pushed_titles
# ============================================================

class TestRecentPushedTitles:
    def test_local_file_recent_only(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GIST_TOKEN", raising=False)
        monkeypatch.delenv("GIST_ID", raising=False)
        monkeypatch.setattr(fc, "_REALTIME_STATE_PATH", tmp_path / "real_time_state.json")
        now = datetime.now()
        recent = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        old = (now - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        state = {"seen": {
            "a": {"t": recent, "pushed": True, "title": "日本央行加息"},
            "b": {"t": old, "pushed": True, "title": "五小时前的旧推送"},
            "c": {"t": recent, "pushed": False, "title": "未推消息"},
            "d": {"t": "bad-time", "pushed": True, "title": "时间格式异常"},
        }}
        (tmp_path / "real_time_state.json").write_text(json.dumps(state, ensure_ascii=False),
                                                       encoding="utf-8")
        titles = fc._recent_pushed_titles()
        assert titles == ["日本央行加息"]

    def test_limit_and_missing_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GIST_TOKEN", raising=False)
        monkeypatch.delenv("GIST_ID", raising=False)
        monkeypatch.setattr(fc, "_REALTIME_STATE_PATH", tmp_path / "not_exist.json")
        assert fc._recent_pushed_titles() == []

        now = datetime.now()
        recent = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        state = {"seen": {f"k{i}": {"t": recent, "pushed": True, "title": f"标题{i}"}
                          for i in range(7)}}
        path = tmp_path / "not_exist.json"
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        titles = fc._recent_pushed_titles(limit=5)
        assert len(titles) == 5


# ============================================================
# 盘前简报 / 盘后复盘 dry-run（mock 数据源）
# ============================================================

class _FakeTool:
    """StructuredTool.func 替身"""

    def __init__(self, items):
        self._items = items

    def func(self):
        return list(self._items)


@pytest.fixture()
def local_env(monkeypatch, tmp_path):
    """去掉 Gist 环境变量，强制本地文件分支"""
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    monkeypatch.delenv("GIST_ID", raising=False)
    monkeypatch.setattr(rtp, "_FACTOR_STATE_PATH", tmp_path / "factor_state.json")
    return tmp_path


class TestMorningBrief:
    def test_dry_run_renders_top5(self, local_env, monkeypatch, capsys):
        now = datetime.now(BJT)
        recent = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        old = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        news = [
            {"title": "日本央行暗示进一步加息，日元急升", "content": "套息交易平仓风险",
             "source": "华尔街见闻", "published_at": recent},
            {"title": "美联储会议纪要显示通胀担忧", "content": "利率路径",
             "source": "金十数据", "published_at": recent},
            {"title": "两天前的旧消息应被隔夜窗口过滤", "content": "",
             "source": "旧源", "published_at": old},
        ]
        monkeypatch.setattr(rtp, "get_stock_news", _FakeTool(news))
        monkeypatch.setattr(rtp, "get_market_signals", _FakeTool([]))
        monkeypatch.setattr(rtp, "dedup_news_3layer", lambda x: list(x))
        # 快照写入本地 factor_state
        local_env.joinpath("factor_state.json").write_text(
            json.dumps({"risk_state": "risk_off", "snapshot": _sample_snapshot()},
                       ensure_ascii=False), encoding="utf-8")

        stats = rtp.run_morning_brief(dry_run=True)
        out = capsys.readouterr().out

        assert "盘前简报" in out
        assert "日本央行" in out
        assert "两天前的旧消息" not in out
        assert "风险收缩期" in out
        assert stats["overnight_candidates"] == 2

    def test_dry_run_no_news(self, local_env, monkeypatch, capsys):
        monkeypatch.setattr(rtp, "get_stock_news", _FakeTool([]))
        monkeypatch.setattr(rtp, "get_market_signals", _FakeTool([]))
        monkeypatch.setattr(rtp, "dedup_news_3layer", lambda x: list(x))

        rtp.run_morning_brief(dry_run=True)
        out = capsys.readouterr().out
        assert "隔夜无重要资讯" in out
        assert "快照缺失" in out

    def test_signal_items_capped_at_two(self, local_env, monkeypatch, capsys):
        """龙虎榜/业绩预告信号最多占 2 席，不挤占要闻（简报是事件综述）"""
        now = datetime.now(BJT)
        recent = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        news = [{"title": f"宏观要闻{i}：美债收益率飙升", "content": "", "source": "金十",
                 "published_at": recent} for i in range(3)]
        signals = [{"title": f"龙虎榜: 股票{i}(60000{i}) 机构净买入1亿元", "content": "",
                    "source": "交易所龙虎榜", "published_at": recent} for i in range(1, 5)]
        monkeypatch.setattr(rtp, "get_stock_news", _FakeTool(news))
        monkeypatch.setattr(rtp, "get_market_signals", _FakeTool(signals))
        monkeypatch.setattr(rtp, "dedup_news_3layer", lambda x: list(x))

        rtp.run_morning_brief(dry_run=True)
        out = capsys.readouterr().out
        assert out.count("龙虎榜:") <= 2
        assert "宏观要闻2" in out


class TestEveningReview:
    def test_dry_run_renders_today_events(self, local_env, monkeypatch, capsys, tmp_path):
        today = datetime.now(BJT).strftime("%Y-%m-%d")
        state = {
            "seen": {
                "fp1": {"t": f"{today} 10:15:00", "pushed": True, "title": "日本央行加息"},
                "fp2": {"t": f"{today} 13:40:00", "pushed": False, "title": "未推消息"},
                "fp3": {"t": "2026-08-01 10:00:00", "pushed": True, "title": "往日已推"},
            },
            "pushed_events": [
                {"t": f"{today} 10:15:00", "dir": "bearish", "sectors": ["全球流动性"]},
                {"t": f"{today} 13:20:00", "dir": "bullish", "sectors": ["半导体", "AI"]},
            ],
            "pending": {},
        }
        state_path = tmp_path / "real_time_state.json"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(rtp, "_state_path", lambda: state_path)
        local_env.joinpath("factor_state.json").write_text(
            json.dumps({"risk_state": "neutral", "snapshot": _sample_snapshot(),
                        "last_direction": "中性"}, ensure_ascii=False), encoding="utf-8")

        stats = rtp.run_evening_review(dry_run=True)
        out = capsys.readouterr().out

        assert "今日已推事件（1 条）" in out
        assert "日本央行加息" in out
        assert "未推消息" not in out
        assert "往日已推" not in out
        assert "利好 1 条｜利空 1 条" in out
        assert "半导体" in out
        assert stats["pushed_events"] == 1

    def test_dry_run_empty_state(self, local_env, monkeypatch, capsys, tmp_path):
        state_path = tmp_path / "real_time_state.json"
        state_path.write_text(json.dumps({"seen": {}, "pushed_events": [], "pending": {}}),
                              encoding="utf-8")
        monkeypatch.setattr(rtp, "_state_path", lambda: state_path)

        rtp.run_evening_review(dry_run=True)
        out = capsys.readouterr().out
        assert "今日无推送" in out
