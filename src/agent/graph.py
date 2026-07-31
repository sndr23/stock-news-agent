# filepath: src/agent/graph.py
"""
A股资讯监测 Agent 图组装
基于 LangGraph 1.0 的 StateGraph 构建
"""
from langgraph.graph import StateGraph, START, END

from src.agent.state import AgentState, create_initial_state
from src.agent.nodes import (
    fetch_news_node,
    prefilter_node,
    llm_filter_node,
    route_after_prefilter,
    rank_news_node,
)


def build_agent():
    """构建A股资讯监测Agent

    工作流:
    1. fetch_news: 抓取当日全部新闻+公告
    2. prefilter: Python 预筛选 (去噪去重 + 重要度初筛)
    3. 条件路由决策 (若全为常规流水账则智能跳过 LLM)
    4. llm_filter: 条目级分流 LLM 标签化分析
    5. rank_news: 全局打分排序与衰减

    注: 不使用 checkpointer。原 MemorySaver 在每次 run_agent 调用时随
    build_agent() 重新创建,状态从不跨调用保留,thread_id 参数从未真正生效,
    属于误导性死设计,已移除。
    """
    workflow = StateGraph(AgentState)

    workflow.add_node("fetch_news", fetch_news_node)
    workflow.add_node("prefilter", prefilter_node)
    workflow.add_node("llm_filter", llm_filter_node)
    workflow.add_node("rank_news", rank_news_node)

    workflow.add_edge(START, "fetch_news")
    workflow.add_edge("fetch_news", "prefilter")

    # 增加条件路由连线
    workflow.add_conditional_edges(
        "prefilter",
        route_after_prefilter,
        {
            "go_to_llm": "llm_filter",
            "skip_to_rank": "rank_news"
        }
    )

    workflow.add_edge("llm_filter", "rank_news")
    workflow.add_edge("rank_news", END)

    app = workflow.compile()
    return app


def run_agent(
    data_mode: str = "live",
    thread_id: str = "default"
):
    """运行Agent生成排名结果

    thread_id: 保留参数以兼容 API/脚本调用签名,当前无实际作用
               (无 checkpointer,每次运行均为独立全新状态)。
    """
    # 启动时校验配置，fail fast（避免 LLM 调用时才发现 API Key 缺失）
    from src.config import validate_config
    validate_config()
    agent = build_agent()
    initial_state = create_initial_state(data_mode)
    result = agent.invoke(initial_state)
    return result
