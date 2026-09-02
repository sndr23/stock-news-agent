# -*- coding: utf-8 -*-
"""创业板择时 · 端到端推送链路测试（E2E）

背景：三代生产事故（--push --shadow 短路致云端从不推送；推送后 update_shadow_history
KeyError 致状态不写/影子不积累；ERP 语义反转）全部逃过了纯单测——单测只测纯函数，
从不走 main() 的完整推送链路。本文件用 mock 数据层（无网络）驱动 main() 全流程，
把"推送→状态写→影子记录→去重"这条链路关进测试门禁。

覆盖场景：
  1. --push 首次：推送 + 状态写（last_date/position/pending）+ 影子记录
  2. --push 同日重复：last_date 去重跳过（不重复推送）
  3. --push 推送失败：状态不更新（明日重跑，防漏推）
  4. --push --shadow：推送正常（短路回归，曾致云端从不推送）
  5. --shadow 纯报告：打印多期影子 IC（不推送不写状态）
"""
import datetime as _dt
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

pytestmark = pytest.mark.unit

import pandas as pd

import run_chinext_timing as rct
from src.strategy import data as sdata


class _FakeDT(_dt.datetime):
    """固定时钟：2026-08-24（周一）14:45。"""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 24, 14, 45)


def _make_df(n: int = 320, end: str = "2026-08-21") -> pd.DataFrame:
    """399006 合成日线（收盘/成交额/高低），末根为 08-21（周五，完整日）。"""
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    closes = [100.0 * (1 + 0.001 * i) for i in range(n)]
    return pd.DataFrame({
        "close": closes,
        "amount": [1e8 + i * 1e6 for i in range(n)],
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
    }, index=dates)


def _patch_all(monkeypatch, push_ok: bool = True):
    """把 main() 依赖的 数据层/推送/状态 全部替换为可控替身。"""
    monkeypatch.setattr(rct, "datetime", _FakeDT)
    monkeypatch.setattr(rct, "load_index_sina", lambda *a, **k: _make_df())
    monkeypatch.setattr(rct, "load_index_daily_full", lambda *a, **k: _make_df())
    monkeypatch.setattr(rct, "get_quotes", lambda *a, **k: {})
    monkeypatch.setattr(rct, "load_stock_sina", lambda *a, **k: None)
    monkeypatch.setattr(rct.nl, "load_factor_state", lambda: {})
    monkeypatch.setattr(rct.nl, "load_citic_pos_state", lambda: {})
    monkeypatch.setattr(rct.nl, "load_realtime_state", lambda: {})
    monkeypatch.setattr(rct.ovs, "load_overseas", lambda *a, **k: {})
    monkeypatch.setattr(rct, "_load_erp_basis", lambda *a, **k: None)

    sent = []
    monkeypatch.setattr(
        rct, "push_report",
        lambda content, title: (sent.append(title), True)[1] if push_ok else
        (sent.append(title), False)[1])

    states = []
    monkeypatch.setattr(rct, "save_state", lambda s: states.append(dict(s)))
    # 2026-08-24 修复：漏 mock load_state → 场景1/3/4 读真实 Gist state，
    # 真实 last_date 撞上固定时钟（2026-08-24）→ 去重跳过 → 门禁失败 → 云端不推送。
    # 默认空 state（场景2/5 各自再覆盖），与数据层 mock 保持一致，隔离真实状态。
    monkeypatch.setattr(rct, "load_state", lambda: {})
    return sent, states


def _run(argv):
    old = sys.argv
    sys.argv = ["run_chinext_timing.py"] + argv
    try:
        rct.main()
    except SystemExit:
        pass
    finally:
        sys.argv = old


def test_console_configuration_replaces_unsupported_report_characters(monkeypatch):
    """Windows GBK 控制台应启用替换策略，报告打印不能因符号编码崩溃。"""
    calls = {}

    class _Console:
        encoding = "gbk"

        def reconfigure(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(rct.sys, "stdout", _Console())

    rct._configure_stdout()

    assert calls == {"errors": "replace"}


def test_load_state_gist_failure_does_not_fallback_to_local(monkeypatch, tmp_path):
    """Gist 读取失败时不得使用本地旧状态，避免误去重后漏推或回写覆盖。"""
    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(rct, "_LOCAL_STATE_PATH", tmp_path / "chinext_timing_state.json")
    rct._LOCAL_STATE_PATH.write_text(
        '{"last_date": "2026-08-24", "position": 1.0}', encoding="utf-8")

    def fake_urlopen(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(rct.nl, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="Gist.*读取失败"):
        rct.load_state()


def test_load_state_rejects_non_object_local_state(monkeypatch, tmp_path):
    """无 Gist 配置时，本地状态根节点不是对象应安全降级为空。"""
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    monkeypatch.delenv("GIST_ID", raising=False)
    monkeypatch.setattr(rct, "_LOCAL_STATE_PATH", tmp_path / "chinext_timing_state.json")
    rct._LOCAL_STATE_PATH.write_text("[]", encoding="utf-8")

    assert rct.load_state() == {}


def test_local_env_loader_reads_project_env_without_overriding_process_env(
        monkeypatch, tmp_path):
    """本地择时入口读取项目 .ENV，但外部环境变量优先。"""
    env_path = tmp_path / ".ENV"
    env_path.write_text(
        "GIST_TOKEN=file-token\nGIST_ID=file-id\n", encoding="utf-8")
    monkeypatch.setattr(rct, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    monkeypatch.setenv("GIST_ID", "process-id")

    rct._load_local_env()

    assert rct.os.environ["GIST_TOKEN"] == "file-token"
    assert rct.os.environ["GIST_ID"] == "process-id"


def test_save_state_local_write_failure_is_reported(monkeypatch, tmp_path):
    """本地状态写盘失败时必须返回失败，且不得破坏旧状态。"""
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    monkeypatch.delenv("GIST_ID", raising=False)
    path = tmp_path / "chinext_timing_state.json"
    path.write_text('{"position": 1.0}', encoding="utf-8")
    monkeypatch.setattr(rct, "_LOCAL_STATE_PATH", path)

    def fail_replace(*args, **kwargs):
        raise OSError("replace failed")

    from src.strategy import state_io
    monkeypatch.setattr(state_io.os, "replace", fail_replace)

    assert rct.save_state({"position": 0.0}) is False
    assert path.read_text(encoding="utf-8") == '{"position": 1.0}'


def test_e2e_push_first_time(monkeypatch):
    """场景1：--push 首次 → 推送 + 状态写（含影子记录）+ 档位推进。"""
    sent, states = _patch_all(monkeypatch)
    _run(["--push"])
    assert len(sent) == 1, "应推送一次"
    assert states, "应写状态"
    st = states[-1]
    assert st["last_date"] == "2026-08-24"
    assert "position" in st and "pending" in st
    hist = st.get("history") or []
    assert hist, "影子记录应写入"
    h = hist[-1]
    assert h["date"] == "2026-08-24"
    assert "raw" in h and h["raw"] == {"basis_min_ap": None, "main_net": None,
                                       "down_pct": None, "pcr": None}


def test_e2e_push_same_day_dedup(monkeypatch):
    """场景2：--push 同日重复 → last_date 去重跳过，不重复推送。"""
    sent, states = _patch_all(monkeypatch)
    monkeypatch.setattr(rct, "load_state",
                        lambda: {"last_date": "2026-08-24", "position": 0.6})
    _run(["--push"])
    assert sent == [], "同日已推送过，应去重跳过"
    assert states == [], "不应再写状态"


def test_e2e_push_failure_keeps_state(monkeypatch):
    """场景3：推送失败 → 状态不更新（last_date 不写，明日重跑）。"""
    sent, states = _patch_all(monkeypatch, push_ok=False)
    _run(["--push"])
    assert sent, "应尝试推送"
    assert states == [], "推送失败不得写状态（否则会误记 last_date 导致漏推）"


def test_main_skips_non_trading_day_push_before_fetch(monkeypatch):
    """工作日节假日被外部调度误触发时，不得获取数据或推送节假日信号。"""
    monkeypatch.setattr(rct, "_is_trading_day", lambda: False)
    monkeypatch.setattr(
        rct,
        "load_index_sina",
        lambda *args, **kwargs: pytest.fail("非交易日不应进入行情获取"),
    )
    _run(["--push"])


def test_e2e_state_write_failure_is_not_reported_as_success(monkeypatch):
    """推送成功但状态写入失败时，任务必须失败，不能冒充已持久化。"""
    _patch_all(monkeypatch)
    monkeypatch.setattr(rct, "save_state", lambda state: False)

    old = sys.argv
    sys.argv = ["run_chinext_timing.py", "--push"]
    try:
        with pytest.raises(RuntimeError, match="状态写入失败"):
            rct.main()
    finally:
        sys.argv = old


def test_e2e_push_with_shadow_flag(monkeypatch):
    """场景4：--push --shadow（云端 workflow 实际命令）→ 推送正常。

    回归：曾因 `if args.shadow: return` 短路，云端从不推送/状态不写/影子不积累。
    """
    sent, states = _patch_all(monkeypatch)
    _run(["--push", "--shadow"])
    assert len(sent) == 1, "--push --shadow 必须推送"
    assert states, "--push --shadow 必须写状态"
    assert states[-1]["last_date"] == "2026-08-24"


def test_e2e_snapshot_only_blocks_d_minus_1_fallback(monkeypatch):
    """严格盘中模式缺当日 bar 时不推送、不写状态，禁止静默跑 d-1。"""
    sent, states = _patch_all(monkeypatch)
    monkeypatch.setattr(sdata, "fetch_intraday_bar_tencent", lambda *args: None)

    _run(["--push", "--snapshot-only"])

    assert sent == []
    assert states == []


def test_e2e_shadow_report_only(monkeypatch):
    """场景5：--shadow 纯报告 → 不推送不写状态（影子 IC 报告模式）。"""
    sent, states = _patch_all(monkeypatch)
    monkeypatch.setattr(rct, "load_state",
                        lambda: {"history": [{"date": "2026-08-21", "core": 0.5,
                                              "next_ret": 0.01}]})
    _run(["--shadow"])
    assert sent == [], "纯 shadow 不推送"
    assert states == [], "纯 shadow 不写状态"


def test_main_rejects_insufficient_index_history(monkeypatch, capsys):
    """主指数历史不足核心 warmup 时退出，不输出伪中性信号。"""
    _patch_all(monkeypatch)
    monkeypatch.setattr(rct, "load_index_sina", lambda *a, **k: _make_df(n=10))
    monkeypatch.setattr(rct, "load_index_daily_full", lambda *a, **k: _make_df(n=10))

    old = sys.argv
    sys.argv = ["run_chinext_timing.py", "--dry-run"]
    try:
        with pytest.raises(SystemExit, match="历史不足"):
            rct.main()
    finally:
        sys.argv = old


def test_main_rejects_history_insufficient_after_partial_bar(monkeypatch):
    """原始数据恰有 62 根但含当日盘中 bar 时，完整历史只有 61 根，必须停推。"""
    _patch_all(monkeypatch)
    base = _make_df(n=61)
    partial = base.iloc[[-1]].copy()
    partial.index = pd.DatetimeIndex([pd.Timestamp("2026-08-24")])
    df = pd.concat([base, partial])
    monkeypatch.setattr(rct, "load_index_sina", lambda *a, **k: df)
    monkeypatch.setattr(rct, "load_index_daily_full", lambda *a, **k: df)

    old = sys.argv
    sys.argv = ["run_chinext_timing.py", "--dry-run"]
    try:
        with pytest.raises(SystemExit, match="实际 61 根完整日线"):
            rct.main()
    finally:
        sys.argv = old
