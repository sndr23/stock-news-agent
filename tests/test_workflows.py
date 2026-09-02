"""关键生产 workflow 的调度兜底回归测试。"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKFLOW_ROOT = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _workflow_text(name: str) -> str:
    return (WORKFLOW_ROOT / name).read_text(encoding="utf-8")


def test_factor_workflow_has_pre_signal_schedule_fallback():
    """外部 cron 失联时，14:45 前仍至少采集一次当日增强因子。"""
    text = _workflow_text("realtime-factor.yml")

    assert "schedule:" in text
    assert 'cron: "15 6 * * 1-5"' in text  # UTC 06:15 = 北京时间 14:15
    assert "workflow_dispatch:" in text
    assert "group: realtime-factor" in text


def test_timing_workflow_keeps_external_dispatch_as_signal_trigger():
    """创业板信号不能被提前的 schedule 抢跑，继续由外部服务准点触发。"""
    text = _workflow_text("chinext-timing.yml")

    assert "schedule:" not in text
    assert "workflow_dispatch:" in text
    assert "group: chinext-timing" in text


def test_timing_workflow_exposes_strict_snapshot_manual_mode():
    """手动复验可以选择严格盘中模式，自动任务默认仍走降级链。"""
    text = _workflow_text("chinext-timing.yml")

    assert "snapshot_only:" in text
    assert "--snapshot-only" in text
    assert "default: false" in text


def test_citic_retry_workflow_does_not_sleep_after_final_attempt():
    """第4次数据未就绪后应立即失败，不再无效等待10分钟。"""
    text = _workflow_text("citic-pos-push.yml")

    assert 'if [ "$i" -lt 4 ]; then' in text
    assert text.index('if [ "$i" -lt 4 ]; then') < text.index("sleep 600")
