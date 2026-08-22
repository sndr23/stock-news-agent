# filepath: src/agent/state.py
"""
A股资讯监测 Agent 状态定义
基于 LangGraph 1.0 的 StateGraph 状态模型
"""
from typing import TypedDict, Annotated, Literal
try:
    # LangGraph 1.0 reducer 仅用于 AgentState.messages 注解元数据；
    # 云端 production 不装 langgraph，降级为恒等 reducer，保证类型模块可 import（CI 门禁依赖）。
    from langgraph.graph.message import add_messages
except ImportError:  # pragma: no cover - 仅无 langgraph 的云端环境触发
    def add_messages(left, right):
        if isinstance(right, list):
            return left + right
        return (left or []) + [right]

NO_DATA_SENTINEL = "NO_DATA_AVAILABLE"


class RankedNewsItem(TypedDict):
    """排名后的资讯项"""
    title: str
    source: str
    content: str
    published_at: str
    credibility_score: float
    market_impact_score: float
    cluster_weight: float
    time_factor: float
    total_score: float
    category: str
    sentiment: Literal["bullish", "bearish", "neutral"]
    impact_direction: Literal["bullish", "bearish", "neutral"]
    affected_sectors: list
    affected_stocks: list
    impact_reason: str
    impact_band: str
    band_priority: int
    confidence: str
    influence_scope: str
    analysis_chain: str


class AgentState(TypedDict):
    """A股资讯监测 Agent 状态定义"""
    messages: Annotated[list, add_messages]
    data_mode: Literal["live", "mock"]
    raw_news: list
    announcements: list
    prefiltered_news: list
    filtered_news: list
    ranked_news: list[RankedNewsItem]
    data_status: str


def create_initial_state(data_mode: str = "live") -> AgentState:
    """创建初始状态"""
    return AgentState(
        messages=[],
        data_mode=data_mode,
        raw_news=[],
        announcements=[],
        prefiltered_news=[],
        filtered_news=[],
        ranked_news=[],
        data_status="ok"
    )
