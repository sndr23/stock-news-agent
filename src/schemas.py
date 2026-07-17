# filepath: src/schemas.py
"""
LLM 分析结构化输出 Schema
借鉴 TradingAgents SentimentReport（6-band + 0-10 + confidence）+ DSA 结构化字段
"""
from enum import Enum
from pydantic import BaseModel, Field


class ImpactBand(str, Enum):
    """6 档影响方向 band"""
    BULLISH = "bullish"
    MILDLY_BULLISH = "mildly_bullish"
    NEUTRAL = "neutral"
    MIXED = "mixed"
    MILDLY_BEARISH = "mildly_bearish"
    BEARISH = "bearish"


class Confidence(str, Enum):
    """置信度三档"""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class NewsAnalysisItem(BaseModel):
    """单条资讯的 LLM 分析结果"""
    title: str
    source: str = ""
    content: str = ""
    published_at: str = ""
    category: str = "news"
    market_impact_score: float = Field(
        ge=0.0, le=10.0, description="市场影响力 0-10，0无影响 10极重大"
    )
    impact_band: ImpactBand = Field(description="6档影响方向，须与 score 区间一致")
    confidence: Confidence = Field(description="置信度：high多源/有数据/官方; medium单一来源; low内容不足")
    affected_sectors: list[str] = Field(default=[], description="影响板块，必填")
    affected_stocks: list[str] = Field(default=[], description="明确提及的个股")
    impact_reason: str = Field(default="", description="一句话影响逻辑")
    sentiment: str = Field(default="", description="与 impact_band 对齐")


class NewsAnalysisBatch(BaseModel):
    """一批资讯的分析结果"""
    filtered_news: list[NewsAnalysisItem]
    removed_count: int = 0
    analysis_summary: str = ""
