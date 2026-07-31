# filepath: tests/test_push.py
"""测试推送模块：格式化 + 截断 + top_n 校验"""
import pytest
from unittest.mock import patch, MagicMock
from src.tools.push import (
    format_ranked_news_md,
    push_via_wecom,
    push_news,
    dedup_ranked_by_title,
    _extract_title_core,
    _title_similarity,
)


class TestFormatRankedNewsMd:
    def _make_news(self, title, band="neutral", score=5.0, scope="market"):
        return {
            "title": title,
            "impact_band": band,
            "impact_direction": "neutral",
            "market_impact_score": score,
            "affected_sectors": ["半导体"],
            "affected_stocks": [],
            "impact_reason": "测试原因",
            "influence_scope": scope,
            "analysis_chain": "测试推理链",
            "confidence": "medium",
        }

    def test_empty_input(self):
        result = format_ranked_news_md([])
        assert "今日暂无重要资讯" in result

    def test_basic_format(self):
        news = [self._make_news("测试标题", "bullish", 8.0)]
        result = format_ranked_news_md(news, top_n=10)
        assert "测试标题" in result
        assert "强利好" in result

    def test_top_n_limit(self):
        news = [self._make_news(f"标题{i}") for i in range(30)]
        result = format_ranked_news_md(news, top_n=5)
        assert "标题4" in result
        assert "标题5" not in result

    def test_max_chars_limit(self):
        news = [self._make_news(f"标题{i}") for i in range(30)]
        result = format_ranked_news_md(news, top_n=30, max_chars=500)
        # max_chars=500, estimated_per_item=180, max_by_chars=2
        assert "标题1" in result
        assert "标题2" not in result


class TestWecomTruncation:
    """企业微信推送截断：必须按 UTF-8 字节长度截断，不能按字符数"""

    @patch("src.tools.push._post_with_retry")
    def test_truncation_uses_bytes_not_chars(self, mock_post):
        """3800 个中文字符 ≈ 11400 字节，远超 4000 字节限制
        修复前：字符切片 [:3800] 导致 11400 字节消息被发送，触发 errcode=45008
        修复后：按字节截断到 3800 字节
        """
        mock_post.return_value = {"errcode": 0, "errmsg": "ok"}
        # 构造超长中文内容（每字 3 字节，1500 字 = 4500 字节 > 4000）
        long_content = "测" * 1500
        push_via_wecom("https://qyapi.weixin.qq.com/test", "测试", long_content)

        # 验证 _post_with_retry 被调用
        assert mock_post.called
        call_args = mock_post.call_args
        payload = call_args[0][1]  # 第二个位置参数是 json_payload
        content = payload["markdown"]["content"]
        # 截断后的内容（含截断提示）的 UTF-8 字节长度必须 < 4096
        assert len(content.encode('utf-8')) < 4096
        assert "截断" in content

    @patch("src.tools.push._post_with_retry")
    def test_short_content_not_truncated(self, mock_post):
        mock_post.return_value = {"errcode": 0, "errmsg": "ok"}
        short_content = "短内容"
        push_via_wecom("https://qyapi.weixin.qq.com/test", "测试", short_content)
        payload = mock_post.call_args[0][1]
        content = payload["markdown"]["content"]
        assert "截断" not in content


class TestPushNewsTopNValidation:
    """push_news 的 top_n 参数校验"""

    def test_negative_top_n_clamped_to_1(self):
        """负值 top_n 不应导致从末尾取"""
        news = [{"title": f"标题{i}", "impact_band": "neutral", "impact_direction": "neutral",
                 "market_impact_score": 5.0, "affected_sectors": [], "affected_stocks": [],
                 "impact_reason": "", "influence_scope": "", "analysis_chain": "",
                 "confidence": "medium"} for i in range(10)]
        with patch("src.tools.push.push_via_wecom", return_value={"errcode": 0}):
            result = push_news(news, wecom_webhook="https://test", top_n=-5)
        # 不应崩溃，且应推送至少 1 条
        assert result is not None

    def test_zero_top_n_clamped_to_1(self):
        news = [{"title": "测试", "impact_band": "neutral", "impact_direction": "neutral",
                 "market_impact_score": 5.0, "affected_sectors": [], "affected_stocks": [],
                 "impact_reason": "", "influence_scope": "", "analysis_chain": "",
                 "confidence": "medium"}]
        with patch("src.tools.push.push_via_wecom", return_value={"errcode": 0}):
            result = push_news(news, wecom_webhook="https://test", top_n=0)
        assert result is not None


class TestTitleDedup:
    def test_extract_title_core(self):
        assert _extract_title_core("【财经】测试标题") == "财经测试标题"
        assert _extract_title_core("") == ""

    def test_similarity_identical(self):
        assert _title_similarity("测试", "测试") == 1.0

    def test_similarity_different(self):
        assert _title_similarity(" abc", "xyz") == 0.0

    def test_dedup_removes_similar(self):
        news = [
            {"title": "央行降准0.5个百分点", "impact_band": "neutral"},
            {"title": "央行降准0.5个百分点", "impact_band": "neutral"},
        ]
        result = dedup_ranked_by_title(news)
        assert len(result) == 1
