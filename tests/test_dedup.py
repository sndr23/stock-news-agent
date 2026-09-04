# filepath: tests/test_dedup.py
import pytest
"""测试去重工具：URL 规范化、SimHash、日期窗口"""
from datetime import datetime, timedelta
from src.tools.data_fetchers import (
    _normalize_url, _simhash, _hamming, _in_news_window,
    dedup_news_3layer, NO_DATA_SENTINEL, BJT,
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
        today = datetime.now(BJT).strftime("%Y-%m-%d")
        assert _in_news_window(today) is True

    def test_future_rejected(self):
        future = (datetime.now(BJT) + timedelta(days=2)).strftime("%Y-%m-%d")
        assert _in_news_window(future) is False

    def test_yesterday_passes_with_window1(self):
        yesterday = (datetime.now(BJT) - timedelta(days=1)).strftime("%Y-%m-%d")
        assert _in_news_window(yesterday, look_back_days=1) is True

    def test_old_date_rejected(self):
        old = (datetime.now(BJT) - timedelta(days=10)).strftime("%Y-%m-%d")
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


# ============================================================
# 推送前"同股+同事件"语义去重 (push.dedup_ranked_by_event)
# ============================================================

from src.tools.push import dedup_ranked_by_event, _event_signature


def _mk(title, stocks=None, content="", name=""):
    return {"title": title, "content": content,
            "affected_stocks": stocks or [], "name": name}


class TestEventSignature:
    def test_stock_from_affected_stocks(self):
        stocks, events, numbers = _event_signature(_mk("寒武纪股权激励", stocks=["寒武纪"]))
        assert "寒武纪" in stocks
        assert "激励" in events

    def test_stock_from_name_field(self):
        stocks, _, _ = _event_signature(_mk("公告标题", name="中际旭创"))
        assert "中际旭创" in stocks

    def test_no_event_keywords(self):
        _, events, _ = _event_signature(_mk("某公司召开股东大会"))
        assert events == set()

    def test_group_matching_variants(self):
        # "限制性股票" 与 "股权激励" 同组
        _, e1, _ = _event_signature(_mk("寒武纪股权激励大消息"))
        _, e2, _ = _event_signature(_mk("寒武纪:2026年限制性股票激励计划(草案)"))
        assert e1 == e2 == {"激励"}


class TestDedupRankedByEvent:
    def test_same_stock_same_event_dedup(self):
        """推送实证: 寒武纪股权激励新闻+草案公告+考核办法 3 条 → 保留第 1 条"""
        news = [
            _mk("寒武纪股权激励大消息!业绩考核目标2026-2028累计营收不低于1000亿", stocks=["寒武纪"]),
            _mk("寒武纪:2026年限制性股票激励计划(草案)摘要公告", stocks=["寒武纪"]),
            _mk("寒武纪:2026年限制性股票激励计划实施考核管理办法", stocks=["寒武纪"]),
        ]
        result = dedup_ranked_by_event(news)
        assert len(result) == 1
        assert "大消息" in result[0]["title"]

    def test_same_stock_different_event_kept(self):
        """同股不同事件(回购 vs 业绩) 不去重"""
        news = [
            _mk("中际旭创:董事长提议回购公司股份公告", stocks=["中际旭创"]),
            _mk("中际旭创业绩预告净利润预增", stocks=["中际旭创"]),
        ]
        result = dedup_ranked_by_event(news)
        assert len(result) == 2

    def test_no_stock_not_deduped(self):
        """无个股信息的条目不参与去重"""
        news = [
            _mk("半导体板块股权激励潮起"),
            _mk("半导体公司股权激励密集落地"),
        ]
        result = dedup_ranked_by_event(news)
        assert len(result) == 2

    def test_no_event_not_deduped(self):
        """同股但无事件关键词(章程/决议) 不去重"""
        news = [
            _mk("寒武纪:公司章程", stocks=["寒武纪"]),
            _mk("寒武纪:董事会决议公告", stocks=["寒武纪"]),
        ]
        result = dedup_ranked_by_event(news)
        assert len(result) == 2

    def test_different_stocks_same_event_kept(self):
        """不同个股同一事件类型 不去重"""
        news = [
            _mk("寒武纪股权激励计划", stocks=["寒武纪"]),
            _mk("中际旭创股权激励计划", stocks=["中际旭创"]),
        ]
        result = dedup_ranked_by_event(news)
        assert len(result) == 2

    def test_empty_and_order_preserved(self):
        assert dedup_ranked_by_event([]) == []
        news = [
            _mk("A公司回购股份", stocks=["A公司"]),
            _mk("B公司回购股份", stocks=["B公司"]),
            _mk("A公司回购进展公告", stocks=["A公司"]),
        ]
        result = dedup_ranked_by_event(news)
        assert [n["title"] for n in result] == ["A公司回购股份", "B公司回购股份"]


class TestDedupMarketDrop:
    """行情下跌类去重：同板块/同个股的"盘前走低"近重复应合并（实证：存储芯片走低5条→1条）"""

    def test_same_stock_market_drop_dedup(self):
        news = [
            _mk("美股存储芯片板块盘前走低 SK海力士跌超4%", stocks=["SK海力士", "美光科技"]),
            _mk("美股存储芯片板块盘前走低", stocks=["SK海力士", "美光科技"]),
            _mk("财联社电，美股存储芯片板块盘前走低，SK海力士跌4.4%", stocks=["SK海力士", "美光科技"]),
        ]
        result = dedup_ranked_by_event(news)
        assert len(result) == 1

    def test_different_stock_drop_kept(self):
        """不同个股的下跌新闻不去重"""
        news = [
            _mk("美光科技盘前走低跌3%", stocks=["美光科技"]),
            _mk("寒武纪盘前走低跌2%", stocks=["寒武纪"]),
        ]
        result = dedup_ranked_by_event(news)
        assert len(result) == 2


class TestDedupCoreNumber:
    """P1 修复: 同股 + 同核心金额(亿/万量级) → 不同措辞的近重复也应去重

    实证: 行云科技"算力服务补充协议 30.53 亿"以 3 条不同标题并存，旧逻辑因措辞差异未合并。
    """

    def test_same_stock_same_amount_dedup(self):
        news = [
            _mk("行云科技:关于签订算力服务补充协议的公告", stocks=["行云科技"],
                content="合同金额30.53亿元"),
            _mk("行云科技签订30.53亿算力服务补充协议", stocks=["行云科技"], content=""),
            _mk("行云科技:算力服务补充协议(二)", stocks=["行云科技"],
                content="交易总额30.53亿"),
        ]
        result = dedup_ranked_by_event(news)
        assert len(result) == 1

    def test_different_amount_kept(self):
        """同股但金额不同 → 视为不同事件，保留"""
        news = [
            _mk("某公司中标5亿元大单", stocks=["某公司"], content=""),
            _mk("某公司再签8亿元订单", stocks=["某公司"], content=""),
        ]
        result = dedup_ranked_by_event(news)
        assert len(result) == 2

    def test_amount_unit_mismatch_not_merged(self):
        """同数值不同量词(亿 vs 万) 不互相碰撞"""
        news = [
            _mk("某公司签30亿大单", stocks=["某公司"], content=""),
            _mk("某公司回购30万股", stocks=["某公司"], content=""),
        ]
        result = dedup_ranked_by_event(news)
        assert len(result) == 2

    def test_price_or_percent_not_captured(self):
        """股价/百分比不含 亿/万 量词 → 不触发金额去重（避免误并）"""
        news = [
            _mk("某公司股价涨30%", stocks=["某公司"], content=""),
            _mk("某公司股价30元创新高", stocks=["某公司"], content=""),
        ]
        result = dedup_ranked_by_event(news)
        assert len(result) == 2


# ============================================================
# 同股公告限额 (calculators.cap_announcements_per_stock)
# ============================================================

from src.tools.calculators import cap_announcements_per_stock, dedup_and_cap_for_display



def _ann(title, stocks=None, name=None, score=0.5):
    return {"title": title, "category": "announcement",
            "affected_stocks": stocks or [], "name": name, "total_score": score}


class TestCapAnnouncementsPerStock:
    def test_same_stock_capped(self):
        """寒武纪 3 条常规公告 → 保留按排序前 2 条"""
        news = [
            _ann("寒武纪:股权激励草案", stocks=["寒武纪"], score=0.85),
            _ann("寒武纪:董事会决议", stocks=["寒武纪"], score=0.30),
            _ann("寒武纪:公司章程", stocks=["寒武纪"], score=0.17),
        ]
        out = cap_announcements_per_stock(news, max_per_stock=2)
        assert len(out) == 2

    def test_title_prefix_extracts_stock(self):
        """无 affected_stocks 但标题带 '股票名：' 前缀 → 仍可限额"""
        news = [
            _ann("寒武纪:法律意见书", name=None, stocks=[], score=0.17),
            _ann("寒武纪:股东会通知", name=None, stocks=[], score=0.17),
            _ann("寒武纪:董事会决议", name=None, stocks=[], score=0.17),
        ]
        out = cap_announcements_per_stock(news, max_per_stock=2)
        assert len(out) == 2

    def test_different_stocks_not_capped_together(self):
        news = [
            _ann("A公司:董事会决议", stocks=["A公司"], score=0.3),
            _ann("A公司:公司章程", stocks=["A公司"], score=0.17),
            _ann("B公司:董事会决议", stocks=["B公司"], score=0.3),
            _ann("B公司:公司章程", stocks=["B公司"], score=0.17),
        ]
        out = cap_announcements_per_stock(news, max_per_stock=2)
        assert len(out) == 4  # 各自限额内

    def test_news_category_not_capped(self):
        news = [_ann("某新闻涨停", stocks=["寒武纪"], score=0.9)]
        news[0]["category"] = "news"
        out = cap_announcements_per_stock(news, max_per_stock=2)
        assert len(out) == 1

    def test_zero_cap_disabled(self):
        news = [_ann("寒武纪:董事会决议", stocks=["寒武纪"]),
                _ann("寒武纪:公司章程", stocks=["寒武纪"])]
        out = cap_announcements_per_stock(news, max_per_stock=0)
        assert len(out) == 2


class TestDedupAndCapForDisplay:
    def test_hanwujicombined(self):
        """端到端: 寒武纪 5 股权激励同事件 + 4 常规 → 事件去重(5→1) + 限额(→2)"""
        news = [
            _ann("寒武纪:限制性股票激励计划(草案)摘要", stocks=["寒武纪"], score=0.855),
            _ann("寒武纪:限制性股票激励计划实施考核办法", stocks=["寒武纪"], score=0.601),
            _ann("寒武纪:限制性股票激励计划激励对象名单", stocks=["寒武纪"], score=0.601),
            _ann("寒武纪:法律意见书", stocks=[], name=None, score=0.177),
            _ann("寒武纪:公司章程", stocks=[], name=None, score=0.177),
            _ann("寒武纪:股东会通知", stocks=[], name=None, score=0.177),
            _ann("寒武纪:董事会决议", stocks=[], name=None, score=0.177),
            _ann("寒武纪:核查意见", stocks=[], name=None, score=0.177),
            _ann("寒武纪:变更注册资本公告", stocks=["寒武纪"], score=0.316),
        ]
        out = dedup_and_cap_for_display(news)
        assert len(out) == 2
        assert "草案" in out[0]["title"]

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用


# ============================================================
# 2026-09-04 新增新闻源：第一财经 + 东财行业资讯
# ============================================================

class TestNewSources0904:
    """新增源解析与接入验证（mock requests，无网络）。"""

    def _today(self):
        return datetime.now(BJT).strftime("%Y-%m-%d")

    def test_yicai_parses_items(self, monkeypatch):
        from src.tools import data_fetchers as df
        payload = [{
            "NewsTitle": "中际旭创：实控人解除质押135万股",
            "CreateDate": f"{self._today()}T19:16:56",
            "NewsNotes": "中际旭创公告摘要",
            "NewsSource": "第一财经",
            "NewsUrl": "https://www.yicai.com/news/1.html",
        }]

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        monkeypatch.setattr(df.requests, "get", lambda *a, **k: FakeResp())
        out = df._fetch_yicai_news()
        assert len(out) == 1
        assert out[0]["title"] == "中际旭创：实控人解除质押135万股"
        assert out[0]["source"] == "第一财经"
        assert out[0]["published_at"] == f"{self._today()}T19:16:56"

    def test_yicai_empty_payload(self, monkeypatch):
        from src.tools import data_fetchers as df

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return []

        monkeypatch.setattr(df.requests, "get", lambda *a, **k: FakeResp())
        assert df._fetch_yicai_news() == []

    def test_yicai_filters_old_items(self, monkeypatch):
        """超出当日窗口的条目被过滤。"""
        from src.tools import data_fetchers as df
        old = (datetime.now(BJT) - timedelta(days=10)).strftime("%Y-%m-%d")
        payload = [{
            "NewsTitle": "旧闻",
            "CreateDate": f"{old}T10:00:00",
            "NewsNotes": "",
            "NewsSource": "第一财经",
            "NewsUrl": "",
        }]

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        monkeypatch.setattr(df.requests, "get", lambda *a, **k: FakeResp())
        assert df._fetch_yicai_news() == []

    def test_em_industry_parses_both_columns(self, monkeypatch):
        """两个 column（科技+个股公告）都被抓取。"""
        from src.tools import data_fetchers as df
        payload = {"data": {"list": [{
            "title": "群联：明年NAND Flash供应仍吃紧",
            "showTime": f"{self._today()} 19:00:41",
            "summary": "AI带动的半导体需求才刚开始",
            "mediaName": "财联社",
            "url": "http://finance.eastmoney.com/news/1.html",
        }]}}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        monkeypatch.setattr(df.requests, "get", lambda *a, **k: FakeResp())
        out = df._fetch_em_industry_news()
        assert len(out) == len(df._EM_INDUSTRY_COLUMNS)  # 每个 column 各 1 条
        assert out[0]["source"] == "财联社"
        assert out[0]["title"] == "群联：明年NAND Flash供应仍吃紧"

    def test_em_industry_filters_old_items(self, monkeypatch):
        from src.tools import data_fetchers as df
        old = (datetime.now(BJT) - timedelta(days=10)).strftime("%Y-%m-%d")
        payload = {"data": {"list": [{
            "title": "旧闻",
            "showTime": f"{old} 10:00:00",
            "summary": "",
            "mediaName": "财联社",
            "url": "",
        }]}}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        monkeypatch.setattr(df.requests, "get", lambda *a, **k: FakeResp())
        assert df._fetch_em_industry_news() == []

    def test_new_sources_registered_in_get_stock_news(self):
        """两个新源已接入 get_stock_news 的并行抓取。"""
        import inspect
        from src.tools import data_fetchers as df
        fn = df.get_stock_news.func if hasattr(df.get_stock_news, "func") else df.get_stock_news
        src = inspect.getsource(fn)
        assert "_fetch_yicai_news" in src
        assert "_fetch_em_industry_news" in src
        assert "_fetch_watchlist_announcements" in src


# ============================================================
# 2026-09-04 持仓公告定向源（watchlist 代码批量直查）
# ============================================================

class TestWatchlistAnnouncements:
    """持仓公告源：按代码定向 + 例行程过滤 + 窗口过滤（mock，无网络）。"""

    def _today(self):
        return datetime.now(BJT).strftime("%Y-%m-%d")

    def _fake_resp(self, payload):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload
        return FakeResp()

    def test_parses_and_carries_stock_name(self, monkeypatch):
        from src.tools import data_fetchers as df
        payload = {"data": {"list": [{
            "title": "新易盛:关于回购公司股份的公告",
            "display_time": f"{self._today()} 18:30:00",
            "notice_date": f"{self._today()} 00:00:00",
            "art_code": "AN202609041234",
            "codes": [{"stock_code": "300502"}],
        }]}}
        monkeypatch.setattr(df, "_watchlist_codes",
                            lambda: [{"code": "300502", "name": "新易盛"}])
        monkeypatch.setattr(df.requests, "get", lambda *a, **k: self._fake_resp(payload))
        out = df._fetch_watchlist_announcements()
        assert len(out) == 1
        assert out[0]["source"] == "持仓公告"
        assert out[0]["affected_stocks"] == ["新易盛"]
        assert "300502" in out[0]["url"] and "AN202609041234" in out[0]["url"]

    def test_routine_announcements_filtered(self, monkeypatch):
        from src.tools import data_fetchers as df
        payload = {"data": {"list": [
            {"title": "中际旭创:H股公告(翌日披露报表)",
             "display_time": f"{self._today()} 08:00:00", "codes": [{"stock_code": "300308"}]},
            {"title": "南亚新材:2026年第三次临时股东会会议资料",
             "display_time": f"{self._today()} 08:00:00", "codes": [{"stock_code": "688519"}]},
            {"title": "中际旭创:关于实际控制人部分股票解除质押的公告",
             "display_time": f"{self._today()} 09:00:00", "codes": [{"stock_code": "300308"}]},
        ]}}
        monkeypatch.setattr(df, "_watchlist_codes",
                            lambda: [{"code": "300308", "name": "中际旭创"}])
        monkeypatch.setattr(df.requests, "get", lambda *a, **k: self._fake_resp(payload))
        out = df._fetch_watchlist_announcements()
        assert len(out) == 1 and "解除质押" in out[0]["title"]

    def test_old_announcements_filtered(self, monkeypatch):
        from src.tools import data_fetchers as df
        old = (datetime.now(BJT) - timedelta(days=10)).strftime("%Y-%m-%d")
        payload = {"data": {"list": [{
            "title": "新易盛:重大合同公告",
            "display_time": f"{old} 10:00:00", "codes": [{"stock_code": "300502"}],
        }]}}
        monkeypatch.setattr(df, "_watchlist_codes",
                            lambda: [{"code": "300502", "name": "新易盛"}])
        monkeypatch.setattr(df.requests, "get", lambda *a, **k: self._fake_resp(payload))
        assert df._fetch_watchlist_announcements() == []

    def test_empty_watchlist_skips(self, monkeypatch):
        from src.tools import data_fetchers as df
        called = []
        monkeypatch.setattr(df, "_watchlist_codes", lambda: [])
        monkeypatch.setattr(df.requests, "get", lambda *a, **k: called.append(1))
        assert df._fetch_watchlist_announcements() == []
        assert not called

    def test_api_failure_returns_empty(self, monkeypatch):
        from src.tools import data_fetchers as df

        def boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(df, "_watchlist_codes",
                            lambda: [{"code": "300308", "name": "中际旭创"}])
        monkeypatch.setattr(df.requests, "get", boom)
        assert df._fetch_watchlist_announcements() == []
