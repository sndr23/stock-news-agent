# filepath: tests/test_prefilter_flow.py
"""测试预筛流程 _python_prefilter 改造后行为"""
from src.agent.nodes import _python_prefilter


def test_basic_filter_keeps_important():
    news = [
        {"title": "贵州茅台业绩预增200%", "content": "业绩大增", "url": "", "source": "财联社"},
        {"title": "某公司庆典活动", "content": "周年庆", "url": "", "source": "自媒体"},
        {"title": "央行降准释放流动性", "content": "大盘利好", "url": "", "source": "新华社"},
    ]
    kept, removed = _python_prefilter(news, top_n=40)
    titles = [n["title"] for n in kept]
    assert "某公司庆典活动" not in titles
    assert len(kept) <= 40


def test_direct_category_quota():
    news = [
        {"title": f"600519事件{i}", "content": "业绩预增", "url": "", "source": ""}
        for i in range(50)
    ]
    kept, removed = _python_prefilter(news, top_n=40)
    assert len(kept) <= 40


def test_empty_input():
    kept, removed = _python_prefilter([], top_n=40)
    assert kept == []
    assert removed == 0


def test_cluster_weight_preserved():
    news = [
        {"title": "半导体板块大涨", "content": "", "url": "", "source": ""},
        {"title": "半导体板块大涨持续", "content": "", "url": "", "source": ""},
    ]
    kept, removed = _python_prefilter(news, top_n=40)
    for n in kept:
        assert "cluster_weight" in n
