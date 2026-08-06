# filepath: tests/test_state.py
import pytest
"""测试 AgentState 初始状态包含 data_status"""
from src.agent.state import create_initial_state, NO_DATA_SENTINEL



def test_initial_state_has_data_status_ok():
    state = create_initial_state("live")
    assert state["data_status"] == "ok"


def test_no_data_sentinel_constant():
    assert NO_DATA_SENTINEL == "NO_DATA_AVAILABLE"

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
