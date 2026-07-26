# filepath: tests/test_dedup.py
"""测试去重工具：URL 规范化、SimHash、日期窗口"""
from datetime import datetime, timedelta
from src.tools.data_fetchers import (
    _normalize_url, _simhash, _hamming, _in_news_window,
    dedup_news_3layer, NO_DATA_SENTINEL,
)


class TestNormalizeUrl:
    def test_basic(self):
        assert _normalize_url("HTTPS://WWW.Example.com/path/?q=1#frag") == "https://example.com/path"

    def test_strip_www(self):
        assert _normalize_url("https://www.sse.com.cn/disclosure/") == "https://sse.com.cn/disclosure"

    def test_empty(self):
        assert _normalize_url("") == ""

    def test_trailing_slash(self):
        assert _normalize_url("https://a.com/") == "https://a.com"


class TestSimHash:
    def test_identical_text_same_hash(self):
        assert _simhash("半导体板块大涨") == _simhash("半导体板块大涨")

    def test_similar_text_small_distance(self):
        h1 = _simhash("半导体板块大涨")
        h2 = _simhash("半导体板块大涨！")
        assert _hamming(h1, h2) <= 10

    def test_different_text_large_distance(self):
        h1 = _simhash("半导体板块大涨")
        h2 = _simhash("央行降准释放流动性")
        assert _hamming(h1, h2) > 5

    def test_empty_text(self):
        assert _simhash("") == 0


class TestInNewsWindow:
    def test_today_passes(self):
        today = datetime.now().strftime("%Y-%m-%d")
        assert _in_news_window(today) is True

    def test_future_rejected(self):
        future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        assert _in_news_window(future) is False

    def test_yesterday_passes_with_window1(self):
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        assert _in_news_window(yesterday, look_back_days=1) is True

    def test_old_date_rejected(self):
        old = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        assert _in_news_window(old, look_back_days=1) is False

    def test_empty_rejected(self):
        assert _in_news_window("") is False


class TestDedup3Layer:
    def test_url_dedup(self):
        news = [
            {"title": "标题A", "url": "https://a.com/path?q=1", "content": ""},
            {"title": "标题A不同", "url": "https://a.com/path?q=2", "content": ""},
        ]
        result = dedup_news_3layer(news)
        assert len(result) == 1

    def test_title_exact_dedup(self):
        news = [
            {"title": "相同标题", "url": "", "content": ""},
            {"title": "相同标题", "url": "", "content": ""},
        ]
        result = dedup_news_3layer(news)
        assert len(result) == 1

    def test_simhash_near_dedup(self):
        news = [
            {"title": "半导体板块大涨创历史新高", "url": "https://x.com/1", "content": ""},
            {"title": "半导体板块大涨创历史新高！", "url": "https://y.com/2", "content": ""},
        ]
        result = dedup_news_3layer(news, simhash_threshold=3)
        assert len(result) == 1

    def test_keep_both_different(self):
        news = [
            {"title": "半导体板块大涨", "url": "https://x.com/1", "content": ""},
            {"title": "央行降准释放流动性", "url": "https://y.com/2", "content": ""},
        ]
        result = dedup_news_3layer(news)
        assert len(result) == 2
