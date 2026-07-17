# filepath: src/agent/state.py
"""
A股资讯监测 Agent 状态定义
基于 LangGraph 1.0 的 StateGraph 状态模型
"""
from typing import TypedDict, Annotated, Literal
from langgraph.graph.message import add_messages

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
