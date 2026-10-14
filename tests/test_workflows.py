"""关键生产 workflow 的调度兜底回归测试。"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKFLOW_ROOT = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _workflow_text(name: str) -> str:
    return (WORKFLOW_ROOT / name).read_text(encoding="utf-8")






