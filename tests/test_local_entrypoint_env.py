# -*- coding: utf-8 -*-
"""本地脚本入口配置加载回归测试。"""
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import check_timing_reconcile as reconcile  # noqa: E402
import run_fund_rotation as fund_rotation  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("module", [fund_rotation, reconcile])
def test_local_entrypoint_loads_project_env_without_overriding_process_env(
        module, monkeypatch, tmp_path):
    """本地入口应读取项目 .ENV，且已有进程环境变量优先。"""
    (tmp_path / ".ENV").write_text(
        "GIST_TOKEN=file-token\nGIST_ID=file-id\n", encoding="utf-8")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    monkeypatch.setenv("GIST_ID", "process-id")

    module._load_local_env()

    assert module.os.environ["GIST_TOKEN"] == "file-token"
    assert module.os.environ["GIST_ID"] == "process-id"


def test_reconcile_gist_only_does_not_read_or_print_local_state(
        monkeypatch, capsys):
    """--gist-only 只应核验云端，避免本地损坏状态干扰审计输出。"""
    monkeypatch.setenv("GIST_TOKEN", "token")
    monkeypatch.setenv("GIST_ID", "gist")
    monkeypatch.setattr(reconcile, "_read_gist", lambda token, gist_id: {
        "last_date": "2026-08-27", "position": 0.0, "history": []})

    def fail_local_read():
        raise AssertionError("--gist-only 不应读取本地状态")

    monkeypatch.setattr(reconcile, "_read_local", fail_local_read)
    monkeypatch.setattr("sys.argv", ["check_timing_reconcile.py", "--gist-only"])

    reconcile.main()

    output = capsys.readouterr().out
    assert "gist" in output
    assert "local" not in output


def test_reconcile_gist_only_without_config_does_not_read_local_state(
        monkeypatch, capsys):
    """缺少 Gist 配置时 --gist-only 应明确中止，不回退到本地。"""
    # 其他测试模块可能在收集阶段从项目 .ENV 载入配置；显式置空后，
    # 入口的 override=False 也不会重新加载它们。
    monkeypatch.setenv("GIST_TOKEN", "")
    monkeypatch.setenv("GIST_ID", "")

    def fail_local_read():
        raise AssertionError("--gist-only 缺少云端配置时不应读取本地状态")

    monkeypatch.setattr(reconcile, "_read_local", fail_local_read)
    monkeypatch.setattr("sys.argv", ["check_timing_reconcile.py", "--gist-only"])

    reconcile.main()

    output = capsys.readouterr().out
    assert "ABORT" in output
    assert "GIST_TOKEN/GIST_ID" in output


@pytest.mark.parametrize("name, value", [("GIST_TOKEN", "token"),
                                          ("GIST_ID", "gist")])
def test_reconcile_partial_gist_config_aborts_without_reading_local_state(
        monkeypatch, capsys, name, value):
    """对账入口遇到半配置时必须中止，不能把错误配置当成本地模式。"""
    monkeypatch.setenv("GIST_TOKEN", "")
    monkeypatch.setenv("GIST_ID", "")
    monkeypatch.setenv(name, value)

    def fail_local_read():
        raise AssertionError("Gist 配置不完整时不应读取本地状态")

    monkeypatch.setattr(reconcile, "_read_local", fail_local_read)
    monkeypatch.setattr("sys.argv", ["check_timing_reconcile.py"])

    reconcile.main()

    output = capsys.readouterr().out
    assert "ABORT" in output
    assert "GIST_TOKEN/GIST_ID" in output


def test_reconcile_reports_malformed_gist_state_without_traceback(
        monkeypatch, capsys):
    """云端 JSON 损坏时应输出可操作的中止信息。"""
    monkeypatch.setenv("GIST_TOKEN", "token")
    monkeypatch.setenv("GIST_ID", "gist")
    monkeypatch.setattr(
        reconcile, "_read_gist",
        lambda token, gist_id: (_ for _ in ()).throw(ValueError("bad json")),
    )
    monkeypatch.setattr("sys.argv", ["check_timing_reconcile.py", "--gist-only"])

    reconcile.main()

    output = capsys.readouterr().out
    assert "ABORT" in output
    assert "云端状态读取失败" in output
    assert "Traceback" not in output
