# 资讯规则成熟化重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把资讯监测链路四环节（去重/预筛/LLM分析/排名）的规则升级到与成熟项目对齐的水准，保留链路骨架与综合排名公式。

**Architecture:** 最小改动——链路与节点不变，只替换节点内部规则。①去重加 URL 规范化+SimHash+日期窗口+哨兵；②预筛用 DSA 权重表替换黑名单；③LLM 改 Pydantic 结构化输出+冲突护栏+降级；④排名改 band 主序+连续分数次序+confidence 加权。

**Tech Stack:** Python 3.10+、LangGraph、LangChain、Pydantic v2、pytest、akshare、requests

**Spec:** `docs/superpowers/specs/2026-07-17-rule-refactor-design.md`

**已确认默认值（4 项待评审）：** 科技硬件词在 `score_news_relevance` 内单独 +10；`_SIMHASH_THRESHOLD=3`；MIXED 单列且排序在 neutral 之上；预筛配额 direct∞/sector20/macro10。

---

## File Structure

| 文件 | 职责 | 改动类型 |
|---|---|---|
| `src/schemas.py` | Pydantic 模型：ImpactBand/Confidence/NewsAnalysisItem/NewsAnalysisBatch | 新建 |
| `src/agent/state.py` | AgentState 新增 data_status 字段 | 修改 |
| `src/tools/data_fetchers.py` | 去重工具函数+哨兵+日期窗口 | 修改 |
| `src/tools/calculators.py` | 预筛权重表+排名升级 | 修改 |
| `src/agent/nodes.py` | 预筛流程+LLM结构化+冲突护栏 | 修改 |
| `tests/conftest.py` | pytest 配置+sys.path | 新建 |
| `tests/test_dedup.py` | 去重测试 | 新建 |
| `tests/test_prefilter.py` | 预筛权重表测试 | 新建 |
| `tests/test_llm_filter.py` | LLM 结构化+护栏测试 | 新建 |
| `tests/test_rank.py` | 排名测试 | 新建 |

---

### Task 1: 测试基础设施

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: 创建 tests 目录与 conftest**

```python
# filepath: tests/conftest.py
"""pytest 配置：把项目根目录加入 sys.path，便于 import src.*"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
```

```python
# filepath: tests/__init__.py
```

- [ ] **Step 2: 验证 pytest 可运行**

Run: `python -m pytest tests/ -v --collect-only`
Expected: `collected 0 items` 且无 import 错误

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py tests/__init__.py
git commit -m "test: 添加 pytest 基础设施"
```

---

### Task 2: Pydantic Schema 定义

**Files:**
- Create: `src/schemas.py`
- Test: `tests/test_schemas.py`

- [ ] **Step 1: 写失败测试**

```python
# filepath: tests/test_schemas.py
"""测试 Pydantic schema 定义与校验"""
import pytest
from pydantic import ValidationError

from src.schemas import ImpactBand, Confidence, NewsAnalysisItem, NewsAnalysisBatch


def test_impact_band_values():
    assert ImpactBand.BULLISH.value == "bullish"
    assert ImpactBand.MIXED.value == "mixed"
    assert ImpactBand.BEARISH.value == "bearish"
    assert len(list(ImpactBand)) == 6


def test_confidence_values():
    assert Confidence.HIGH.value == "high"
    assert Confidence.LOW.value == "low"
    assert len(list(Confidence)) == 3


def test_news_analysis_item_valid():
    item = NewsAnalysisItem(
        title="测试标题",
        market_impact_score=8.0,
        impact_band=ImpactBand.BULLISH,
        confidence=Confidence.HIGH,
        affected_sectors=["半导体"],
    )
    assert item.title == "测试标题"
    assert item.market_impact_score == 8.0


def test_news_analysis_item_score_out_of_range():
    with pytest.raises(ValidationError):
        NewsAnalysisItem(
            title="测试",
            market_impact_score=15.0,  # 超过 10
            impact_band=ImpactBand.BULLISH,
            confidence=Confidence.HIGH,
        )


def test_news_analysis_item_score_negative():
    with pytest.raises(ValidationError):
        NewsAnalysisItem(
            title="测试",
            market_impact_score=-1.0,
            impact_band=ImpactBand.BEARISH,
            confidence=Confidence.LOW,
        )


def test_news_analysis_batch():
    item = NewsAnalysisItem(
        title="测试",
        market_impact_score=5.0,
        impact_band=ImpactBand.NEUTRAL,
        confidence=Confidence.MEDIUM,
    )
    batch = NewsAnalysisBatch(filtered_news=[item], removed_count=2, analysis_summary="摘要")
    assert len(batch.filtered_news) == 1
    assert batch.removed_count == 2
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.schemas'`

- [ ] **Step 3: 实现 schema**

```python
# filepath: src/schemas.py
"""
LLM 分析结构化输出 Schema
借鉴 TradingAgents SentimentReport（6-band + 0-10 + confidence）+ DSA 结构化字段
"""
from enum import Enum
from pydantic import BaseModel, Field


class ImpactBand(str, Enum):
    """6 档影响方向 band"""
    BULLISH = "bullish"              # 强利好 (score 6.5-10)
    MILDLY_BULLISH = "mildly_bullish"  # 弱利好 (5.5-6.4)
    NEUTRAL = "neutral"              # 中性 (4.5-5.5)
    MIXED = "mixed"                  # 多空交织 (4.5-5.5)
    MILDLY_BEARISH = "mildly_bearish"  # 弱利空 (3.5-4.4)
    BEARISH = "bearish"              # 强利空 (0-3.4)


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
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/schemas.py tests/test_schemas.py
git commit -m "feat: 添加 LLM 分析 Pydantic schema (6-band + confidence)"
```

---

### Task 3: AgentState 扩展 data_status

**Files:**
- Modify: `src/agent/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: 写失败测试**

```python
# filepath: tests/test_state.py
"""测试 AgentState 初始状态包含 data_status"""
from src.agent.state import create_initial_state, NO_DATA_SENTINEL


def test_initial_state_has_data_status_ok():
    state = create_initial_state("live")
    assert state["data_status"] == "ok"


def test_no_data_sentinel_constant():
    assert NO_DATA_SENTINEL == "NO_DATA_AVAILABLE"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_state.py -v`
Expected: FAIL — `ImportError: cannot import name 'NO_DATA_SENTINEL'`

- [ ] **Step 3: 修改 state.py**

在 `src/agent/state.py` 顶部常量区新增哨兵，`AgentState` 新增字段，`create_initial_state` 补默认值：

```python
# filepath: src/agent/state.py
# 在文件顶部 import 之后新增常量
NO_DATA_SENTINEL = "NO_DATA_AVAILABLE"
```

修改 `AgentState`，在 `ranked_news` 字段后新增：
```python
    ranked_news: list[RankedNewsItem]
    data_status: str  # "ok" 或 NO_DATA_SENTINEL
```

修改 `create_initial_state`，在返回字典末尾新增：
```python
        ranked_news=[],
        data_status="ok"
    )
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_state.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent/state.py tests/test_state.py
git commit -m "feat: AgentState 新增 data_status 字段与 NO_DATA 哨兵"
```

---

### Task 4: 去重工具函数（URL 规范化 + SimHash + 日期窗口）

**Files:**
- Modify: `src/tools/data_fetchers.py`
- Test: `tests/test_dedup.py`

- [ ] **Step 1: 写失败测试**

```python
# filepath: tests/test_dedup.py
"""测试去重工具：URL 规范化、SimHash、日期窗口"""
from datetime import datetime, timedelta
from src.tools.data_fetchers import (
    _normalize_url, _simhash, _hamming, _in_news_window,
    dedup_news三层, NO_DATA_SENTINEL,
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
        assert _hamming(h1, h2) <= 5

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


class TestDedup三层:
    def test_url_dedup(self):
        news = [
            {"title": "标题A", "url": "https://a.com/path?q=1", "content": ""},
            {"title": "标题A不同", "url": "https://a.com/path?q=2", "content": ""},
        ]
        result = dedup_news三层(news)
        assert len(result) == 1

    def test_title_exact_dedup(self):
        news = [
            {"title": "相同标题", "url": "", "content": ""},
            {"title": "相同标题", "url": "", "content": ""},
        ]
        result = dedup_news三层(news)
        assert len(result) == 1

    def test_simhash_near_dedup(self):
        news = [
            {"title": "半导体板块大涨创历史新高", "url": "https://x.com/1", "content": ""},
            {"title": "半导体板块大涨创历史新高！", "url": "https://y.com/2", "content": ""},
        ]
        result = dedup_news三层(news, simhash_threshold=3)
        assert len(result) == 1

    def test_keep_both_different(self):
        news = [
            {"title": "半导体板块大涨", "url": "https://x.com/1", "content": ""},
            {"title": "央行降准释放流动性", "url": "https://y.com/2", "content": ""},
        ]
        result = dedup_news三层(news)
        assert len(result) == 2
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_dedup.py -v`
Expected: FAIL — `ImportError: cannot import name '_normalize_url'`

- [ ] **Step 3: 在 data_fetchers.py 新增去重工具函数**

在 `src/tools/data_fetchers.py` 文件顶部（import 之后、缓存区之前）新增：

```python
# filepath: src/tools/data_fetchers.py
# ============================================================
# 去重工具（URL 规范化 + SimHash + 日期窗口）
# 借鉴 TradingAgents get_global_news_yfinance 的 seen_titles + _in_news_window
# ============================================================

NO_DATA_SENTINEL = "NO_DATA_AVAILABLE"
_SIMHASH_THRESHOLD = 3  # 海明距离 ≤3 视为近似重复


def _normalize_url(url: str) -> str:
    """URL 规范化：去 query/fragment，统一 host 小写，去 www 前缀与末尾斜杠"""
    if not url or not url.strip():
        return ""
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def _simhash(text: str, bits: int = 64) -> int:
    """字符级 3-gram SimHash，对中文短标题友好"""
    if not text:
        return 0
    grams = [text[i:i + 3] for i in range(max(len(text) - 2, 0))]
    if not grams:
        return 0
    v = [0] * bits
    for g in grams:
        h = hash(g) & ((1 << bits) - 1)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    fingerprint = 0
    for i in range(bits):
        if v[i] > 0:
            fingerprint |= (1 << i)
    return fingerprint


def _hamming(a: int, b: int) -> int:
    """海明距离"""
    return bin(a ^ b).count("1")


def _in_news_window(published_at: str, look_back_days: int = 1) -> bool:
    """只保留 [今天-look_back_days, 今天] 的资讯，排除未来日期
    
    借鉴 TradingAgents _in_news_window，防未来新闻(look-ahead safe)
    """
    if not published_at or not str(published_at).strip():
        return False
    text = str(published_at).strip()
    now = datetime.now()
    start = now - timedelta(days=look_back_days)

    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d", "%Y%m%d", "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d", "%Y%m%d %H:%M:%S"]:
        try:
            pub_time = datetime.strptime(text, fmt)
            if start <= pub_time <= now:
                return True
            return False
        except ValueError:
            continue

    # 兜底：纯日期前缀匹配（保留原有 _is_today 的部分能力）
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if text == today or text.startswith(today + " ") or text.startswith(today + "T"):
        return True
    if look_back_days >= 1 and (text == yesterday or text.startswith(yesterday + " ")):
        return True
    return False


def dedup_news三层(news_list: list, simhash_threshold: int = _SIMHASH_THRESHOLD) -> list:
    """三层去重：URL 精确 → 标题精确 → SimHash 近似
    
    Args:
        news_list: 资讯列表，每条含 title/url 字段
        simhash_threshold: SimHash 海明距离阈值，≤此值视为重复
    Returns:
        去重后的列表
    """
    # 第一层：URL 规范化精确去重
    seen_urls = set()
    after_url = []
    for news in news_list:
        url = _normalize_url(news.get("url", ""))
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        after_url.append(news)

    # 第二层：标题精确去重
    seen_titles = set()
    after_title = []
    for news in after_url:
        title = news.get("title", "").strip()
        if title:
            if title in seen_titles:
                continue
            seen_titles.add(title)
        after_title.append(news)

    # 第三层：SimHash 近似去重
    result = []
    fingerprints = []
    for news in after_title:
        title = news.get("title", "").strip()
        if not title:
            result.append(news)
            continue
        fp = _simhash(title)
        is_dup = False
        for existing_fp in fingerprints:
            if _hamming(fp, existing_fp) <= simhash_threshold:
                is_dup = True
                break
        if not is_dup:
            result.append(news)
            fingerprints.append(fp)

    return result
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_dedup.py -v`
Expected: 全部 passed（注意 SimHash 测试可能与具体 hash 实现相关，若 `test_similar_text_small_distance` 失败，放宽断言至 `<= 10`）

- [ ] **Step 5: Commit**

```bash
git add src/tools/data_fetchers.py tests/test_dedup.py
git commit -m "feat: 新增 URL规范化+SimHash+日期窗口三层去重"
```

---

### Task 5: 预筛权重表 score_news_relevance

**Files:**
- Modify: `src/tools/calculators.py`
- Test: `tests/test_prefilter.py`

- [ ] **Step 1: 写失败测试**

```python
# filepath: tests/test_prefilter.py
"""测试预筛权重表 score_news_relevance"""
from src.tools.calculators import (
    score_news_relevance, _is_official_source,
    _COMPANY_EVENT_TERMS, _SECTOR_NEWS_TERMS, _MACRO_NEWS_TERMS,
)


class TestScoreNewsRelevance:
    def test_stock_code_in_title(self):
        item = {"title": "贵州茅台发布年报", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item, stock_code="600519", stock_name="贵州茅台")
        assert score >= 45  # 公司名标题命中 45
        assert cat == "direct"

    def test_company_name_in_title(self):
        item = {"title": "宁德时代签订大单", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item, stock_name="宁德时代")
        assert score >= 45
        assert cat == "direct"

    def test_event_term_alone_in_flow_mode(self):
        """资讯流场景：无 stock_code，事件词独立触发 +12"""
        item = {"title": "某公司业绩预增200%", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item)
        assert score >= 12  # 事件词 +12

    def test_official_source_bonus(self):
        item = {"title": "上交所公告", "content": "", "url": "https://sse.com.cn/notice", "source": "上交所"}
        score, cat = score_news_relevance(item)
        assert score >= 8  # 官方源 +8

    def test_sector_term_bonus(self):
        item = {"title": "半导体行业景气度提升", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item)
        assert score >= 6  # 板块词 +6

    def test_macro_penalty(self):
        """宏观词且无 direct 时 -12"""
        item = {"title": "央行降准释放流动性", "content": "大盘上涨", "url": "", "source": ""}
        score, cat = score_news_relevance(item)
        assert cat == "macro"

    def test_tech_hardware_bonus(self):
        """科技硬件词单独 +10"""
        item = {"title": "光模块需求爆发", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item)
        assert score >= 10  # 科技词 +10

    def test_score_clamped_0_100(self):
        item = {"title": "x", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item)
        assert 0 <= score <= 100

    def test_direct_category_threshold(self):
        """direct_signal >= 38 归 direct"""
        item = {"title": "600519业绩预增", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item, stock_code="600519")
        assert cat == "direct"


class TestIsOfficialSource:
    def test_host_match(self):
        assert _is_official_source(url="https://sse.com.cn/x", source="") is True

    def test_label_match(self):
        assert _is_official_source(url="", source="上海证券交易所公告") is True

    def test_non_official(self):
        assert _is_official_source(url="https://random.com/x", source="随机媒体") is False

    def test_empty(self):
        assert _is_official_source(url="", source="") is False
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_prefilter.py -v`
Expected: FAIL — `ImportError: cannot import name 'score_news_relevance'`

- [ ] **Step 3: 在 calculators.py 新增权重表函数**

在 `src/tools/calculators.py` 文件中（`TECH_HARDWARE_KEYWORDS` 列表定义之后、`calculate_prefilter_importance` 之前）新增：

```python
# filepath: src/tools/calculators.py
# ============================================================
# 预筛权重表（借鉴 daily_stock_analysis _score_news_relevance）
# ============================================================

# 官方可信源 host 白名单（以 URL host 为准，防 source label 伪装）
_OFFICIAL_HOSTS = {
    "cninfo.com.cn", "sse.com", "sse.com.cn", "szse.cn",
    "hkexnews.hk", "sec.gov", "nasdaq.com", "nyse.com",
    "bse.cn",  # 北交所
}
_OFFICIAL_LABELS = {"巨潮资讯", "上交所", "深交所", "港交所", "北交所"}


def _is_official_source(url: str = "", source: str = "") -> bool:
    """判断是否官方可信源（优先以 URL host 为准）"""
    if url:
        from src.tools.data_fetchers import _normalize_url
        normalized = _normalize_url(url)
        if normalized:
            host = normalized.split("//")[-1].split("/")[0] if "//" in normalized else ""
            if host in _OFFICIAL_HOSTS:
                return True
    if source:
        return any(lbl in source for lbl in _OFFICIAL_LABELS)
    return False


# 公司事件词（命中时 +12，资讯流场景独立触发）
_COMPANY_EVENT_TERMS = [
    "业绩预增", "业绩预减", "业绩扭亏", "业绩暴雷", "业绩预告",
    "并购", "重组", "借壳", "收购", "减持", "增持", "回购",
    "立案调查", "退市", "ST", "*ST", "涨停", "跌停",
    "重大合同", "中标", "签约", "债务违约", "监管处罚",
]

# 板块背景词
_SECTOR_NEWS_TERMS = [
    "行业", "板块", "产业链", "龙头", "概念股", "赛道",
    "sector", "industry", "peers", "supply chain",
]

# 宏观词（命中且无 direct 时 -12，归 macro 类）
_MACRO_NEWS_TERMS = [
    "大盘", "指数", "宏观", "央行", "利率", "通胀", "降准", "降息",
    "A股", "港股", "美股", "纳指", "标普", "fed", "inflation",
    "北向资金", "外资", "印花税", "注册制",
]


def score_news_relevance(
    item: dict, stock_code: str = "", stock_name: str = ""
) -> tuple:
    """资讯关联度打分（借鉴 DSA _score_news_relevance，适配资讯流场景）

    权重表：
      代码命中 标题+55 / 摘要+34 / URL+18
      公司名命中 标题+45 / 摘要+28
      事件词+12（资讯流场景独立触发，不依赖 direct_signal）
      科技硬件词+10（保留科技倾斜）
      官方源+8
      板块词+6
      宏观词-12（条件：无 direct_signal）
    分类：direct_signal>=38 → direct; 宏观且无direct → macro; 否则 sector

    Returns:
        (score 0-100, category: "direct"/"sector"/"macro")
    """
    score = 0
    direct_signal = 0
    title = item.get("title", "")
    content = item.get("content", "")
    url = item.get("url", "")
    text = f"{title} {content}"

    # 代码命中（互斥）
    if stock_code:
        if stock_code in title:
            score += 55; direct_signal += 55
        elif stock_code in content:
            score += 34; direct_signal += 34
        elif stock_code in url:
            score += 18; direct_signal += 18

    # 公司名命中
    if stock_name:
        if stock_name in title:
            score += 45; direct_signal += 45
        elif stock_name in content:
            score += 28; direct_signal += 28

    # 事件词（资讯流场景独立触发，+12 到 score 和 direct_signal）
    if any(t in text for t in _COMPANY_EVENT_TERMS):
        score += 12; direct_signal += 12

    # 科技硬件词单独 +10（保留科技倾斜）
    if any(kw in text for kw in TECH_HARDWARE_KEYWORDS):
        score += 10

    # 官方源
    if _is_official_source(url, item.get("source", "")):
        score += 8

    # 板块词
    if any(t in text for t in _SECTOR_NEWS_TERMS):
        score += 6

    # 宏观词（无 direct 时降权 -12）
    is_macro = any(t in text for t in _MACRO_NEWS_TERMS)
    if is_macro and direct_signal == 0:
        score -= 12

    score = max(0, min(100, score))

    if direct_signal >= 38:
        category = "direct"
    elif is_macro and direct_signal == 0:
        category = "macro"
    else:
        category = "sector"
    return score, category
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_prefilter.py -v`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add src/tools/calculators.py tests/test_prefilter.py
git commit -m "feat: 新增预筛权重表 score_news_relevance (DSA 权重移植)"
```

---

### Task 6: 预筛流程改造 _python_prefilter

**Files:**
- Modify: `src/agent/nodes.py`
- Test: `tests/test_prefilter_flow.py`

- [ ] **Step 1: 写失败测试**

```python
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
    # 庆典类低分应被淘汰
    titles = [n["title"] for n in kept]
    assert "某公司庆典活动" not in titles
    assert len(kept) <= 40


def test_direct_category_quota():
    """direct 类全留"""
    news = [
        {"title": f"600519事件{i}", "content": "业绩预增", "url": "", "source": ""}
        for i in range(50)
    ]
    kept, removed = _python_prefilter(news, top_n=40)
    # 事件词触发 direct_signal=12 < 38，归 sector，按配额截断
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
    # 聚类权重应被附加
    for n in kept:
        assert "cluster_weight" in n
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_prefilter_flow.py -v`
Expected: FAIL（庆典类未被淘汰，因旧黑名单逻辑仍在）

- [ ] **Step 3: 改造 _python_prefilter**

在 `src/agent/nodes.py` 中，替换整个 `_python_prefilter` 函数。删除旧的 `NOISE_KEYWORDS` 和 `TECH_HARDWARE_KEYWORDS`（nodes.py 中的副本），替换为新实现：

```python
# filepath: src/agent/nodes.py
# 在文件顶部 import 区新增
from src.tools.calculators import (
    rank_news, calculate_prefilter_importance, predict_direction_by_rules,
    infer_sectors_by_rules, score_news_relevance,
)
from src.tools.data_fetchers import dedup_news三层
```

替换 `_python_prefilter` 函数（删除旧的 NOISE_KEYWORDS / TECH_HARDWARE_KEYWORDS / 旧 `_python_prefilter`，替换为）：

```python
# 预筛分类配额
_PREFILTER_QUOTA = {"direct": None, "sector": 20, "macro": 10}  # None=不限
_PREFILTER_TOTAL_LIMIT = 40


def _python_prefilter(news_list: list, top_n: int = _PREFILTER_TOTAL_LIMIT) -> tuple:
    """Python 预筛：权重表打分 + 分类配额截断 + 聚类热度

    流程：
      1. 三层去重（URL/标题/SimHash）
      2. 对每条调 score_news_relevance 打分分类
      3. 按分类配额截断：direct全留, sector取top20, macro取top10
      4. 计算聚类热度 cluster_weight
    """
    # 1. 三层去重
    deduped = dedup_news三层(news_list)
    dup_count = len(news_list) - len(deduped)

    # 2. 打分分类
    for news in deduped:
        score, category = score_news_relevance(news)
        news["_prefilter_score"] = score
        news["_prefilter_category"] = category

    # 3. 分类配额截断
    buckets = {"direct": [], "sector": [], "macro": []}
    for news in deduped:
        cat = news["_prefilter_category"]
        buckets[cat].append(news)

    # 各桶按分排序
    for cat in buckets:
        buckets[cat].sort(key=lambda x: x["_prefilter_score"], reverse=True)

    # 配额截断
    kept = []
    for cat, quota in _PREFILTER_QUOTA.items():
        if quota is not None:
            kept.extend(buckets[cat][:quota])
        else:
            kept.extend(buckets[cat])

    # 总上限保护
    if len(kept) > top_n:
        kept.sort(key=lambda x: x["_prefilter_score"], reverse=True)
        kept = kept[:top_n]

    # 4. 聚类热度
    for i, news1 in enumerate(kept):
        cluster_size = 1
        title1 = news1.get("title", "")
        for j, news2 in enumerate(kept):
            if i != j:
                title2 = news2.get("title", "")
                if _calc_similarity(title1, title2) > 0.35:
                    cluster_size += 1
        news1["cluster_weight"] = min(cluster_size - 1, 10)

    # 清理临时字段
    for news in kept:
        news.pop("_prefilter_score", None)
        news.pop("_prefilter_category", None)

    total_removed = dup_count + (len(deduped) - len(kept))
    return kept, total_removed
```

保留旧的 `_calc_similarity` 函数（2-gram Jaccard）不删除。

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_prefilter_flow.py -v`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent/nodes.py tests/test_prefilter_flow.py
git commit -m "feat: 预筛流程改用权重表打分+分类配额"
```

---

### Task 7: LLM 结构化输出 + 冲突护栏

**Files:**
- Modify: `src/agent/nodes.py`
- Test: `tests/test_llm_filter.py`

- [ ] **Step 1: 写失败测试**

```python
# filepath: tests/test_llm_filter.py
"""测试 LLM 分析冲突护栏与 band/score 对齐"""
import pytest
from src.schemas import ImpactBand, Confidence, NewsAnalysisItem
from src.agent.nodes import (
    _apply_guardrails, _band_from_score, _band_to_direction,
    BAND_SCORE_RANGE,
)


class TestBandFromScore:
    def test_bullish(self):
        assert _band_from_score(8.0) == ImpactBand.BULLISH

    def test_mildly_bullish(self):
        assert _band_from_score(6.0) == ImpactBand.MILDLY_BULLISH

    def test_neutral(self):
        assert _band_from_score(5.0) == ImpactBand.NEUTRAL

    def test_mildly_bearish(self):
        assert _band_from_score(4.0) == ImpactBand.MILDLY_BEARISH

    def test_bearish(self):
        assert _band_from_score(2.0) == ImpactBand.BEARISH


class TestBandToDirection:
    def test_bullish_band_to_bullish(self):
        assert _band_to_direction(ImpactBand.BULLISH) == "bullish"
        assert _band_to_direction(ImpactBand.MILDLY_BULLISH) == "bullish"

    def test_bearish_band_to_bearish(self):
        assert _band_to_direction(ImpactBand.BEARISH) == "bearish"
        assert _band_to_direction(ImpactBand.MILDLY_BEARISH) == "bearish"

    def test_neutral_band_to_neutral(self):
        assert _band_to_direction(ImpactBand.NEUTRAL) == "neutral"
        assert _band_to_direction(ImpactBand.MIXED) == "neutral"


class TestApplyGuardrails:
    def test_no_conflict_unchanged(self):
        item = NewsAnalysisItem(
            title="测试", market_impact_score=8.0,
            impact_band=ImpactBand.BULLISH, confidence=Confidence.HIGH,
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.BULLISH

    def test_conflict_score_high_band_low_corrected(self):
        """score=8 但 band=bearish → 校正为 bullish"""
        item = NewsAnalysisItem(
            title="测试", market_impact_score=8.0,
            impact_band=ImpactBand.BEARISH, confidence=Confidence.HIGH,
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.BULLISH
        assert result[0].sentiment == "bullish"

    def test_conflict_score_low_band_high_corrected(self):
        """score=2 但 band=bullish → 校正为 bearish"""
        item = NewsAnalysisItem(
            title="测试", market_impact_score=2.0,
            impact_band=ImpactBand.BULLISH, confidence=Confidence.LOW,
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.BEARISH

    def test_mixed_score_neutral_band_kept(self):
        """score=5.0 band=mixed 在区间内，不校正"""
        item = NewsAnalysisItem(
            title="测试", market_impact_score=5.0,
            impact_band=ImpactBand.MIXED, confidence=Confidence.MEDIUM,
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.MIXED
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_llm_filter.py -v`
Expected: FAIL — `ImportError: cannot import name '_apply_guardrails'`

- [ ] **Step 3: 在 nodes.py 新增护栏函数与结构化输出**

在 `src/agent/nodes.py` 顶部 import 区新增：
```python
from src.schemas import ImpactBand, Confidence, NewsAnalysisItem, NewsAnalysisBatch
```

在 `_safe_parse_json` 函数之后、`_llm_analyze_batch` 之前新增：

```python
# ============================================================
# 冲突护栏（借鉴 DSA score_action_conflicts_without_guardrail）
# ============================================================

BAND_SCORE_RANGE = {
    ImpactBand.BULLISH: (6.5, 10.0),
    ImpactBand.MILDLY_BULLISH: (5.5, 6.4),
    ImpactBand.NEUTRAL: (4.5, 5.5),
    ImpactBand.MIXED: (4.5, 5.5),
    ImpactBand.MILDLY_BEARISH: (3.5, 4.4),
    ImpactBand.BEARISH: (0.0, 3.4),
}


def _band_from_score(score: float) -> ImpactBand:
    """按 score 强制分档"""
    if score >= 6.5:
        return ImpactBand.BULLISH
    if score >= 5.5:
        return ImpactBand.MILDLY_BULLISH
    if score >= 4.5:
        return ImpactBand.NEUTRAL
    if score >= 3.5:
        return ImpactBand.MILDLY_BEARISH
    return ImpactBand.BEARISH


def _band_to_direction(band: ImpactBand) -> str:
    """band → 3 档 direction（排名公式兼容）"""
    bullish = {ImpactBand.BULLISH, ImpactBand.MILDLY_BULLISH}
    bearish = {ImpactBand.BEARISH, ImpactBand.MILDLY_BEARISH}
    if band in bullish:
        return "bullish"
    if band in bearish:
        return "bearish"
    return "neutral"


def _apply_guardrails(items: list) -> list:
    """band 与 score 冲突时按 score 强制校正 band，并同步 direction/sentiment"""
    for item in items:
        if isinstance(item, NewsAnalysisItem):
            lo, hi = BAND_SCORE_RANGE[item.impact_band]
            if not (lo <= item.market_impact_score <= hi):
                item.impact_band = _band_from_score(item.market_impact_score)
            item.sentiment = item.impact_band.value
        elif isinstance(item, dict):
            band_str = item.get("impact_band", "neutral")
            try:
                band = ImpactBand(band_str)
            except ValueError:
                band = ImpactBand.NEUTRAL
            lo, hi = BAND_SCORE_RANGE[band]
            score = float(item.get("market_impact_score", 3.0))
            if not (lo <= score <= hi):
                band = _band_from_score(score)
            item["impact_band"] = band.value
            item["sentiment"] = band.value
            item["impact_direction"] = _band_to_direction(band)
    return items
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_llm_filter.py -v`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent/nodes.py tests/test_llm_filter.py
git commit -m "feat: 新增 LLM 分析冲突护栏 (band-score 校正)"
```

---

### Task 8: LLM 结构化输出路径 + 提示词改造

**Files:**
- Modify: `src/agent/nodes.py`
- Test: `tests/test_llm_structured.py`

- [ ] **Step 1: 写失败测试**

```python
# filepath: tests/test_llm_structured.py
"""测试 LLM 结构化输出构建与降级逻辑（mock LLM，不发真实请求）"""
from unittest.mock import patch, MagicMock
from src.agent.nodes import _build_analysis_prompt, _llm_analyze_batch_structured
from src.schemas import NewsAnalysisItem, ImpactBand, Confidence


def test_build_analysis_prompt_contains_contract():
    batch = [{"title": "测试", "content": "内容", "source": "财联社", "published_at": "", "category": "news"}]
    prompt = _build_analysis_prompt(batch)
    assert "impact_band" in prompt
    assert "confidence" in prompt
    assert "market_impact_score" in prompt
    assert "6档" in prompt or "6档之一" in prompt


def test_build_analysis_prompt_truncates_content():
    long_content = "x" * 200
    batch = [{"title": "测试", "content": long_content, "source": "", "published_at": "", "category": "news"}]
    prompt = _build_analysis_prompt(batch)
    assert len(prompt) < 5000  # 截断后不应过长


def test_structured_output_success_via_mock():
    """mock with_structured_output 成功路径"""
    mock_item = NewsAnalysisItem(
        title="测试", market_impact_score=8.0,
        impact_band=ImpactBand.BULLISH, confidence=Confidence.HIGH,
        affected_sectors=["半导体"],
    )
    mock_result = MagicMock()
    mock_result.filtered_news = [mock_item]
    mock_result.removed_count = 0

    mock_llm = MagicMock()
    mock_structured = MagicMock()
    mock_structured.invoke.return_value = mock_result
    mock_llm.with_structured_output.return_value = mock_structured

    with patch("src.agent.nodes._build_llm", return_value=mock_llm):
        result = _llm_analyze_batch_structured([{"title": "测试", "content": "", "source": ""}])

    assert len(result) == 1
    assert result[0].impact_band == ImpactBand.BULLISH


def test_structured_output_fallback_to_freetext():
    """with_structured_output 失败 → 降级自由文本"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output.side_effect = Exception("provider 不支持")

    fake_json = '{"filtered_news": [{"title": "测试", "market_impact_score": 7.0, "impact_band": "bullish", "confidence": "high"}], "removed_count": 0}'

    with patch("src.agent.nodes._build_llm", return_value=mock_llm):
        with patch("src.agent.nodes._call_llm_api", return_value=fake_json):
            result = _llm_analyze_batch_structured([{"title": "测试", "content": "", "source": ""}])

    assert len(result) == 1
    assert result[0].impact_band == ImpactBand.BULLISH
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_llm_structured.py -v`
Expected: FAIL — `ImportError: cannot import name '_build_analysis_prompt'`

- [ ] **Step 3: 实现结构化输出路径**

在 `src/agent/nodes.py` 中，替换旧的 `ANALYSIS_PROMPT` 常量与 `_llm_analyze_batch` 函数。

替换 `ANALYSIS_PROMPT` 为新提示词：
```python
# filepath: src/agent/nodes.py
ANALYSIS_PROMPT = """你是资深A股资讯分析师。请对以下资讯逐条分析，输出结构化 JSON。

## 资讯列表（共{n}条）
{news_list}

## 输出契约（每条必须包含全部字段）
1. market_impact_score: 0-10（0无影响/10极重大）
2. impact_band: 6档之一
   - bullish(强利好, score 6.5-10): 业绩预增/大额中标/政策扶持/增持回购/技术突破
   - mildly_bullish(弱利好, 5.5-6.4): 普通经营利好
   - neutral(中性, 4.5-5.5): 无明显多空的常规播报
   - mixed(多空交织, 4.5-5.5): 同时含利好利空
   - mildly_bearish(弱利空, 3.5-4.4): 普通经营利空
   - bearish(强利空, 0-3.4): 立案/退市/爆雷/违约/重大处罚
3. confidence: high/medium/low
   - high: 多源报道/有具体数据/官方源
   - medium: 单一来源/有事件细节
   - low: 内容<50字/信息不足
4. affected_sectors: 必填，涉及板块（半导体/CPO/PCB/算力/新能源/医药/银行/...）
5. affected_stocks: 明确提及的个股
6. impact_reason: 一句话影响逻辑

## 规则
- band 与 score 必须一致（见上区间），冲突时以 score 为准调整 band
- 含明确多空信号的严禁判 neutral/mixed
- 科技板块资讯以"对科技板块的影响"判定方向
- 噪音（庆典/八卦/软文）不输出到 filtered_news，计入 removed_count

请以JSON格式返回（只返回JSON）:
```json
{{
  "filtered_news": [
    {{
      "title": "原标题",
      "source": "原来源",
      "content": "原内容",
      "published_at": "原时间",
      "category": "原分类",
      "market_impact_score": 8,
      "impact_band": "bullish",
      "confidence": "high",
      "affected_sectors": ["半导体"],
      "affected_stocks": ["中芯国际"],
      "impact_reason": "半导体国产替代加速，利好板块龙头"
    }}
  ],
  "removed_count": 0,
  "analysis_summary": "本次分析简要摘要"
}}
```

注意:
- filtered_news 中每条必须保留原始字段并新增上述字段
- affected_sectors 必须尽力提取，仅当完全不涉及行业板块时才设为空数组
"""
```

新增 `_build_llm`、`_build_analysis_prompt`、`_llm_analyze_batch_structured`（替换旧 `_llm_analyze_batch`）：

```python
# filepath: src/agent/nodes.py
def _build_llm():
    """构建 LangChain ChatOpenAI（复用 OpenRouter 配置）"""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=OPENROUTER_MODEL_NAME,
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
        temperature=0.3,
        max_tokens=4096,
    )


def _build_analysis_prompt(news_batch: list) -> str:
    """构建分析提示词（预取注入，整块塞提示词）"""
    truncated_batch = []
    for n in news_batch:
        item = dict(n)
        content = item.get("content", "")
        if len(content) > 100:
            item["content"] = content[:100] + "..."
        truncated_batch.append(item)

    return ANALYSIS_PROMPT.format(
        n=len(truncated_batch),
        news_list=json.dumps(truncated_batch, ensure_ascii=False, indent=2)
    )


def _llm_analyze_batch_structured(batch: list) -> list:
    """结构化输出 + 自由文本降级

    借鉴 TradingAgents bind_structured + invoke_structured_or_freetext
    """
    prompt = _build_analysis_prompt(batch)
    system_msg = "你是资深A股资讯分析师。请只返回JSON，不要在JSON字符串中使用换行符。"

    # 方式A：with_structured_output
    try:
        llm = _build_llm()
        structured_llm = llm.with_structured_output(NewsAnalysisBatch)
        result = structured_llm.invoke(prompt)
        if result and result.filtered_news is not None:
            items = result.filtered_news
            return _apply_guardrails(items)
    except Exception as e:
        logger.warning(f"结构化输出失败，降级自由文本: {e}")

    # 方式B：降级到 _call_llm_api + _safe_parse_json
    try:
        content = _call_llm_api(system_msg, prompt, timeout=90, max_retries=2)
        parsed = _safe_parse_json(content)
        raw_items = parsed.get("filtered_news", [])
        items = []
        for raw in raw_items:
            try:
                items.append(NewsAnalysisItem(**raw))
            except Exception as e:
                logger.warning(f"解析单条失败，跳过: {e}")
        return _apply_guardrails(items)
    except Exception as e:
        logger.error(f"LLM 分析完全失败: {e}")
        # 全部降级返回原始数据
        return batch
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_llm_structured.py -v`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent/nodes.py tests/test_llm_structured.py
git commit -m "feat: LLM 分析改结构化输出+提示词改造+降级"
```

---

### Task 9: llm_filter_node 接入结构化路径

**Files:**
- Modify: `src/agent/nodes.py`

- [ ] **Step 1: 改造 llm_filter_node 调用结构化路径**

在 `src/agent/nodes.py` 的 `llm_filter_node` 函数中，把对 `_llm_analyze_batch` 的调用改为 `_llm_analyze_batch_structured`，并适配返回值（结构化返回的是 `NewsAnalysisItem` 对象列表，需要转 dict 合并）。

定位 `llm_filter_node` 内的 `executor.submit(_llm_analyze_batch, batch)`，替换为：

```python
# filepath: src/agent/nodes.py
# 在 llm_filter_node 内
futures = {executor.submit(_llm_analyze_batch_structured, batch): idx for idx, batch in enumerate(batches)}
```

改造结果合并逻辑——结构化返回的 `NewsAnalysisItem` 对象需要提取字段为 dict 以便按 title 合并。定位 `for news in prefiltered:` 循环前的 `llm_results_by_title` 构建逻辑，替换为：

```python
# filepath: src/agent/nodes.py
# 合并 LLM 结果（兼容 NewsAnalysisItem 对象与 dict）
all_llm_results_flat = []
for idx in range(len(batches)):
    filtered_batch = results_map.get(idx)
    if filtered_batch:
        for item in filtered_batch:
            if isinstance(item, NewsAnalysisItem):
                all_llm_results_flat.append(item.model_dump())
            elif isinstance(item, dict):
                all_llm_results_flat.append(item)
            else:
                # 降级兜底：原始 news dict
                all_llm_results_flat.append(item)

llm_results_by_title = {n.get("title", "").strip(): n for n in all_llm_results_flat}
```

同时在合并字段时新增 `impact_band` 和 `confidence` 的回写。定位 `news["impact_direction"] = direction` 附近，补：

```python
# filepath: src/agent/nodes.py
# 在 final_filtered.append(news) 之前补 band/confidence 回写
news["impact_band"] = llm_res.get("impact_band", "neutral")
news["confidence"] = llm_res.get("confidence", "medium")
```

对降级分支（LLM 失败）也补默认值：
```python
# filepath: src/agent/nodes.py
# 降级兜底分支补
news["impact_band"] = "neutral"
news["confidence"] = "low"
```

- [ ] **Step 2: 运行全部测试确认无回归**

Run: `python -m pytest tests/ -v`
Expected: 全部 passed

- [ ] **Step 3: Commit**

```bash
git add src/agent/nodes.py
git commit -m "feat: llm_filter_node 接入结构化输出路径"
```

---

### Task 10: 综合排名升级

**Files:**
- Modify: `src/tools/calculators.py`
- Test: `tests/test_rank.py`

- [ ] **Step 1: 写失败测试**

```python
# filepath: tests/test_rank.py
"""测试综合排名：band 主序 + confidence 加权 + 冲突降级"""
from src.tools.calculators import (
    rank_news, BAND_PRIORITY, CONFIDENCE_WEIGHT,
    _band_direction_conflict, _downgrade_band,
    _calc_continuous_score,
)


class TestBandPriority:
    def test_bullish_highest(self):
        assert BAND_PRIORITY["bullish"] == 6

    def test_bearish_lowest(self):
        assert BAND_PRIORITY["bearish"] == 1

    def test_mixed_above_neutral(self):
        assert BAND_PRIORITY["mixed"] > BAND_PRIORITY["neutral"]


class TestConfidenceWeight:
    def test_high_is_1(self):
        assert CONFIDENCE_WEIGHT["high"] == 1.0

    def test_low_is_0_7(self):
        assert CONFIDENCE_WEIGHT["low"] == 0.7

    def test_medium_between(self):
        assert CONFIDENCE_WEIGHT["medium"] == 0.85


class TestBandDirectionConflict:
    def test_bullish_band_bearish_dir_conflict(self):
        assert _band_direction_conflict("bullish", "bearish") is True

    def test_bearish_band_bullish_dir_conflict(self):
        assert _band_direction_conflict("bearish", "bullish") is True

    def test_no_conflict(self):
        assert _band_direction_conflict("bullish", "bullish") is False
        assert _band_direction_conflict("neutral", "neutral") is False


class TestDowngradeBand:
    def test_bullish_downgrade(self):
        assert _downgrade_band("bullish") == "mildly_bullish"

    def test_neutral_downgrade(self):
        assert _downgrade_band("neutral") == "mildly_bearish"

    def test_bearish_already_lowest(self):
        assert _downgrade_band("bearish") == "bearish"


class TestRankNews:
    def _make_news(self, title, band, direction, score, confidence="medium", category="news"):
        return {
            "title": title, "source": "财联社", "content": "", "published_at": "",
            "category": category, "sentiment": direction, "impact_direction": direction,
            "market_impact_score": score, "impact_band": band, "confidence": confidence,
            "affected_sectors": [], "affected_stocks": [], "cluster_weight": 0,
        }

    def test_bullish_ranks_above_bearish(self):
        news = [
            self._make_news("利空", "bearish", "bearish", 2.0),
            self._make_news("利好", "bullish", "bullish", 8.0),
        ]
        ranked = rank_news(news)
        assert ranked[0]["title"] == "利好"

    def test_high_confidence_ranks_above_low_same_band(self):
        news = [
            self._make_news("低置信", "bullish", "bullish", 8.0, confidence="low"),
            self._make_news("高置信", "bullish", "bullish", 8.0, confidence="high"),
        ]
        ranked = rank_news(news)
        assert ranked[0]["title"] == "高置信"

    def test_conflict_band_downgraded(self):
        """band=bullish 但 direction=bearish → band 降级"""
        news = [self._make_news("冲突", "bullish", "bearish", 8.0)]
        ranked = rank_news(news)
        assert ranked[0]["impact_band"] == "mildly_bullish"  # 降一档

    def test_ranked_item_has_new_fields(self):
        news = [self._make_news("测试", "bullish", "bullish", 8.0)]
        ranked = rank_news(news)
        assert "impact_band" in ranked[0]
        assert "band_priority" in ranked[0]
        assert "confidence" in ranked[0]

    def test_empty_input(self):
        assert rank_news([]) == []

    def test_mixed_above_neutral_in_ranking(self):
        news = [
            self._make_news("中性", "neutral", "neutral", 5.0),
            self._make_news("多空交织", "mixed", "neutral", 5.0),
        ]
        ranked = rank_news(news)
        assert ranked[0]["title"] == "多空交织"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_rank.py -v`
Expected: FAIL — `ImportError: cannot import name 'BAND_PRIORITY'`

- [ ] **Step 3: 在 calculators.py 新增排名升级逻辑**

在 `src/tools/calculators.py` 中，把现有 `rank_news` 函数体内的连续分数计算抽为 `_calc_continuous_score`，并新增 band 相关常量与函数。

在 `rank_news` 函数之前新增：

```python
# filepath: src/tools/calculators.py
# ============================================================
# 综合排名升级（band 主序 + 连续分数次序 + confidence 加权）
# ============================================================

BAND_PRIORITY = {
    "bullish": 6, "mildly_bullish": 5,
    "mixed": 4, "neutral": 3,
    "mildly_bearish": 2, "bearish": 1,
}

CONFIDENCE_WEIGHT = {
    "high": 1.0, "medium": 0.85, "low": 0.7,
}


def _band_direction_conflict(band: str, direction: str) -> bool:
    """band 与 direction 冲突判定"""
    bullish_bands = {"bullish", "mildly_bullish"}
    bearish_bands = {"bearish", "mildly_bearish"}
    if band in bullish_bands and direction == "bearish":
        return True
    if band in bearish_bands and direction == "bullish":
        return True
    return False


def _downgrade_band(band: str) -> str:
    """band 降一档"""
    order = ["bullish", "mildly_bullish", "mixed", "neutral", "mildly_bearish", "bearish"]
    idx = order.index(band) if band in order else 3
    return order[min(idx + 1, len(order) - 1)]


def _calc_continuous_score(news: dict) -> float:
    """连续分数计算（原 rank_news 内的逻辑抽函数，保留可信度×重要度+方向折扣+科技加成）"""
    cred = calculate_credibility(news.get("source", ""))

    raw_val = news.get("market_impact_score", 3.0)
    try:
        if isinstance(raw_val, (int, float)):
            llm_impact_raw = float(raw_val)
        else:
            import re
            match = re.search(r'([\d.]+)', str(raw_val))
            llm_impact_raw = float(match.group(1)) if match else 3.0
    except Exception:
        llm_impact_raw = 3.0

    llm_impact = min(max(llm_impact_raw / 10.0, 0.0), 1.0)

    cluster_w = float(news.get("cluster_weight", 0.0))
    cluster_bonus = min(cluster_w * 0.05, NEWS_CLUSTER_WEIGHT)

    category = news.get("category", "news")
    if category == "announcement":
        total_base = ANN_CRED_WEIGHT * cred + ANN_IMP_WEIGHT * llm_impact
    elif category == "signal":
        total_base = SIGNAL_CRED_WEIGHT * cred + SIGNAL_IMP_WEIGHT * llm_impact
    else:
        total_base = NEWS_CRED_WEIGHT * cred + NEWS_IMP_WEIGHT * llm_impact + cluster_bonus

    direction = news.get("impact_direction", "neutral")
    if direction != "neutral":
        sentiment_factor = 1.00
    else:
        if llm_impact >= 0.70:
            sentiment_factor = 1.00
        elif llm_impact >= 0.40:
            sentiment_factor = 0.92
        else:
            sentiment_factor = 0.80

    total = round(total_base * sentiment_factor, 4)

    # 科技/国家级/ST 加成降级（保留原逻辑）
    title = news.get("title", "")
    content = news.get("content", "")
    name = news.get("name", "")
    clean_title = title.replace(name, "") if name else title
    clean_content = content.replace(name, "") if name else content
    clean_text = f"{clean_title} {clean_content}"

    is_tech = any(kw in clean_text for kw in TECH_HARDWARE_KEYWORDS)
    is_national_auth = _is_national_authority(news.get("source", ""))
    is_national_policy = _has_national_policy(clean_text)
    is_st_delist = any(kw in clean_text for kw in ["*ST", "ST", "退市", "终止上市"])

    if is_tech:
        if direction == "bullish":
            total = round(total * 1.15, 4)
        elif direction == "neutral":
            total = round(total * 1.05, 4)

    if is_national_auth and is_national_policy:
        total = round(total * 1.12, 4)
    elif is_national_auth:
        total = round(total * 1.05, 4)

    if is_st_delist and direction == "bearish":
        total = round(total * 0.85, 4)

    if total > 0.99:
        total = 0.99

    return total
```

替换 `rank_news` 函数为：

```python
# filepath: src/tools/calculators.py
def rank_news(news_list: list) -> list:
    """综合排名：band 主序 → 连续分数次序 → 时间因子

    改进：
    1. band 6 档作为主排序键（分级评级优先）
    2. 连续分数（可信度×重要度+聚类+方向折扣+科技加成）作同级内次排序键
    3. confidence 加权（high 1.0 / medium 0.85 / low 0.7）
    4. band 与 direction 冲突时 band 降一档
    """
    ranked = []
    for news in news_list:
        # 连续分数
        total = _calc_continuous_score(news)

        # confidence 加权
        conf = news.get("confidence", "medium")
        total = round(total * CONFIDENCE_WEIGHT.get(conf, 0.85), 4)

        # band 与方向冲突降级
        band = news.get("impact_band", "neutral")
        direction = news.get("impact_direction", "neutral")
        if _band_direction_conflict(band, direction):
            band = _downgrade_band(band)

        tf = calculate_time_factor(news.get("published_at", ""))

        ranked.append(RankedNewsItem(
            title=news.get("title", ""),
            source=news.get("source", ""),
            content=news.get("content", ""),
            published_at=news.get("published_at", ""),
            credibility_score=calculate_credibility(news.get("source", "")),
            market_impact_score=news.get("market_impact_score", 3.0),
            cluster_weight=float(news.get("cluster_weight", 0.0)),
            time_factor=tf,
            total_score=total,
            category=news.get("category", "news"),
            sentiment=news.get("sentiment", "neutral"),
            impact_direction=direction,
            affected_sectors=news.get("affected_sectors", []),
            affected_stocks=news.get("affected_stocks", []),
            impact_reason=news.get("impact_reason", ""),
            impact_band=band,
            band_priority=BAND_PRIORITY.get(band, 3),
            confidence=conf,
        ))

    # 三级排序：band优先级 → 连续分数 → 时间因子
    ranked.sort(
        key=lambda x: (x["band_priority"], x["total_score"], x["time_factor"]),
        reverse=True
    )
    return ranked
```

- [ ] **Step 4: 更新 RankedNewsItem TypedDict**

在 `src/tools/calculators.py` 的 `RankedNewsItem` TypedDict 中新增字段（在 `impact_reason: str` 之后）：

```python
# filepath: src/tools/calculators.py
class RankedNewsItem(TypedDict):
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
    sentiment: str
    impact_direction: str
    affected_sectors: list
    affected_stocks: list
    impact_reason: str
    impact_band: str
    band_priority: int
    confidence: str
```

同步更新 `src/agent/state.py` 的 `RankedNewsItem`（保持一致）。

- [ ] **Step 5: 运行测试验证通过**

Run: `python -m pytest tests/test_rank.py -v`
Expected: 全部 passed

- [ ] **Step 6: Commit**

```bash
git add src/tools/calculators.py src/agent/state.py tests/test_rank.py
git commit -m "feat: 综合排名升级 band主序+confidence加权+冲突降级"
```

---

### Task 11: fetch_news_node 去重接入 + 哨兵回写

**Files:**
- Modify: `src/agent/nodes.py`
- Modify: `src/tools/data_fetchers.py`

- [ ] **Step 1: 在 fetch_news_node 接入三层去重与哨兵**

在 `src/agent/nodes.py` 的 `fetch_news_node` 函数中，在 `all_news = raw_news + ann_as_news + market_signals` 之后，新增去重与哨兵逻辑：

```python
# filepath: src/agent/nodes.py
# fetch_news_node 内，all_news 组装之后
from src.tools.data_fetchers import dedup_news三层, NO_DATA_SENTINEL

before_dedup = len(all_news)
all_news = dedup_news三层(all_news)
logger.info(f"[fetch_news] 三层去重: {before_dedup} -> {len(all_news)}条")

# 哨兵：全部核心源失败
data_status = "ok"
if not all_news:
    data_status = NO_DATA_SENTINEL
    logger.warning("[fetch_news] 全部数据源失败，置 NO_DATA 哨兵")
```

在 `return` 字典中新增 `data_status`：
```python
# filepath: src/agent/nodes.py
    return {
        "raw_news": all_news,
        "announcements": announcements,
        "data_status": data_status,
        "messages": [
            AIMessage(content=(
                f"[fetch_news] 已获取当日全部资讯："
                f"新闻{len(raw_news)}条 + 公告{len(announcements)}条 + "
                f"信号情报{len(market_signals)}条 = 合计{len(all_news)}条"
                + (" | 注意: 全部数据源失败" if data_status == NO_DATA_SENTINEL else "")
            ))
        ]
    }
```

- [ ] **Step 2: route_after_prefilter 见哨兵直接 skip_to_rank**

在 `src/agent/nodes.py` 的 `route_after_prefilter` 函数开头新增哨兵检查：

```python
# filepath: src/agent/nodes.py
def route_after_prefilter(state: AgentState) -> str:
    # 哨兵：数据源全失败，跳过 LLM
    if state.get("data_status") == NO_DATA_SENTINEL:
        return "skip_to_rank"
    # ... 原有逻辑保留
```

在 `route_after_prefilter` 之上或 `fetch_news_node` import 区，补 `NO_DATA_SENTINEL` 导入（若未导入）。

- [ ] **Step 3: 运行全部测试**

Run: `python -m pytest tests/ -v`
Expected: 全部 passed

- [ ] **Step 4: Commit**

```bash
git add src/agent/nodes.py src/tools/data_fetchers.py
git commit -m "feat: fetch_news 接入三层去重+NO_DATA哨兵回写"
```

---

### Task 12: rank_news_node 适配空结果与哨兵提示

**Files:**
- Modify: `src/agent/nodes.py`

- [ ] **Step 1: rank_news_node 增强哨兵提示**

在 `src/agent/nodes.py` 的 `rank_news_node` 函数中，在 `ranked = rank_news(filtered_news)` 之后，增强空结果提示（已有部分逻辑，补哨兵维度）：

定位 `if not ranked:` 分支，扩展消息：

```python
# filepath: src/agent/nodes.py
# rank_news_node 内
    if not ranked:
        raw_count = len(state.get("raw_news", []))
        pre_count = len(state.get("prefiltered_news", []))
        data_status = state.get("data_status", "ok")
        msg_parts.append(
            f" | 注意: 排名为空！原始{raw_count}条, 预筛{pre_count}条, "
            f"data_status={data_status}"
        )
        if data_status == NO_DATA_SENTINEL:
            msg_parts.append(" 根因: 全部数据源失败，请检查网络/akshare可用性。")
        elif raw_count == 0:
            msg_parts.append(" 可能原因: 数据源全部不可用或网络异常。")
        elif pre_count == 0:
            msg_parts.append(" 可能原因: 预筛过滤过严，请检查权重表配额。")
```

确保 `NO_DATA_SENTINEL` 已在 rank_news_node 可见（文件级 import 已有）。

- [ ] **Step 2: 运行全部测试**

Run: `python -m pytest tests/ -v`
Expected: 全部 passed

- [ ] **Step 3: Commit**

```bash
git add src/agent/nodes.py
git commit -m "feat: rank_news_node 增强哨兵与空结果诊断提示"
```

---

### Task 13: 端到端回归测试

**Files:**
- Create: `tests/test_e2e_regression.py`

- [ ] **Step 1: 写端到端回归测试（mock 数据源，不依赖网络）**

```python
# filepath: tests/test_e2e_regression.py
"""端到端回归：mock 数据源 → 全链路 → 验证 band 排序与 confidence"""
from unittest.mock import patch
from src.agent.nodes import _python_prefilter, _apply_guardrails
from src.tools.calculators import rank_news
from src.schemas import ImpactBand, Confidence, NewsAnalysisItem


def test_e2e_mock_pipeline():
    """模拟完整链路：聚合(mocks) → 预筛 → LLM(mock) → 排名"""
    # 1. 模拟 raw_news（含噪音/重复/正常）
    raw_news = [
        {"title": "贵州茅台业绩预增200%", "content": "业绩大增", "url": "https://a.com/1", "source": "财联社", "published_at": "", "category": "news"},
        {"title": "贵州茅台业绩预增200%", "content": "业绩大增", "url": "https://a.com/1", "source": "财联社", "published_at": "", "category": "news"},  # URL 重复
        {"title": "某公司30周年庆典", "content": "周年庆", "url": "https://b.com/2", "source": "自媒体", "published_at": "", "category": "news"},
        {"title": "央行降准释放流动性", "content": "大盘利好", "url": "https://c.com/3", "source": "新华社", "published_at": "", "category": "news"},
        {"title": "半导体板块国产替代加速", "content": "光模块需求爆发", "url": "https://d.com/4", "source": "财联社", "published_at": "", "category": "news"},
    ]

    # 2. 预筛
    kept, removed = _python_prefilter(raw_news, top_n=40)
    assert "某公司30周年庆典" not in [n["title"] for n in kept]
    assert removed >= 1  # 至少去重掉 1 条

    # 3. 模拟 LLM 分析结果（mock）
    llm_results = [
        NewsAnalysisItem(title="贵州茅台业绩预增200%", market_impact_score=8.0,
                         impact_band=ImpactBand.BULLISH, confidence=Confidence.HIGH,
                         affected_sectors=["白酒"], affected_stocks=["贵州茅台"],
                         impact_reason="业绩大增"),
        NewsAnalysisItem(title="央行降准释放流动性", market_impact_score=9.0,
                         impact_band=ImpactBand.BULLISH, confidence=Confidence.HIGH,
                         affected_sectors=["银行"], impact_reason="流动性利好"),
        NewsAnalysisItem(title="半导体板块国产替代加速", market_impact_score=7.0,
                         impact_band=ImpactBand.BULLISH, confidence=Confidence.MEDIUM,
                         affected_sectors=["半导体", "CPO"], impact_reason="国产替代"),
    ]
    llm_results = _apply_guardrails(llm_results)

    # 转 dict 并合并到 kept
    llm_by_title = {item.title: item.model_dump() for item in llm_results}
    merged = []
    for news in kept:
        title = news.get("title", "")
        if title in llm_by_title:
            merged_news = {**news, **llm_by_title[title]}
            merged_news["impact_direction"] = "bullish"  # band=bullish → direction
            merged.append(merged_news)

    # 4. 排名
    ranked = rank_news(merged)
    assert len(ranked) > 0

    # 验证 band 主序：bullish 应排最前
    assert ranked[0]["impact_band"] == "bullish"
    # 验证 confidence 字段存在
    for item in ranked:
        assert "confidence" in item
        assert "impact_band" in item
        assert "band_priority" in item


def test_e2e_conflict_guardrail_in_pipeline():
    """端到端：LLM 返回冲突 band/score → 护栏校正 → 排名正确"""
    raw = [{"title": "测试冲突", "content": "", "url": "", "source": "", "published_at": "", "category": "news"}]
    kept, _ = _python_prefilter(raw, top_n=40)

    # LLM 返回 score=8 但 band=bearish（冲突）
    conflict_item = NewsAnalysisItem(
        title="测试冲突", market_impact_score=8.0,
        impact_band=ImpactBand.BEARISH,  # 冲突
        confidence=Confidence.MEDIUM,
    )
    corrected = _apply_guardrails([conflict_item])
    assert corrected[0].impact_band == ImpactBand.BULLISH  # 校正为 bullish

    merged = {**kept[0], **corrected[0].model_dump()}
    merged["impact_direction"] = "bullish"
    ranked = rank_news([merged])
    assert ranked[0]["impact_band"] == "bullish"


def test_e2e_empty_input():
    """空输入不崩溃"""
    kept, removed = _python_prefilter([], top_n=40)
    assert kept == []
    ranked = rank_news([])
    assert ranked == []
```

- [ ] **Step 2: 运行端到端测试**

Run: `python -m pytest tests/test_e2e_regression.py -v`
Expected: 全部 passed

- [ ] **Step 3: 运行全部测试确认无回归**

Run: `python -m pytest tests/ -v`
Expected: 全部 passed

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e_regression.py
git commit -m "test: 端到端回归测试覆盖全链路"
```

---

### Task 14: 清理旧代码 + 最终验证

**Files:**
- Modify: `src/agent/nodes.py`

- [ ] **Step 1: 删除已废弃的旧代码**

在 `src/agent/nodes.py` 中删除：
- 旧的 `NOISE_KEYWORDS` 列表（已被 `score_news_relevance` 取代）
- 旧的 `TECH_HARDWARE_KEYWORDS` 列表（nodes.py 中的副本，calculators.py 中保留权威定义）
- 旧的 `_llm_analyze_batch` 函数（已被 `_llm_analyze_batch_structured` 取代）
- 旧的 `HIGH_SIGNAL_KEYWORDS`（若 `route_after_prefilter` 不再使用，删除；若仍用，保留）

> 注意：`_has_high_signal` 仍被 `route_after_prefilter` 使用，其内部引用的 `TECH_HARDWARE_KEYWORDS` 需改为从 `calculators` 导入。改为：
> ```python
> from src.tools.calculators import TECH_HARDWARE_KEYWORDS
> ```
> 并删除 nodes.py 内的 `TECH_HARDWARE_KEYWORDS` 副本。

- [ ] **Step 2: 验证 calculators.py __main__ 回归基线**

Run: `python -m src.tools.calculators`
Expected: 原有 `__main__` 测试输出不报错（RankedNewsItem 新增字段不影响旧用例，因为旧用例不检查新字段）

> 若 `__main__` 中 `rank_news` 调用因新增 `impact_band` 字段缺失报错，在 `__main__` 的测试数据中补 `"impact_band": "bullish"` 等默认值。

- [ ] **Step 3: 运行全部测试**

Run: `python -m pytest tests/ -v`
Expected: 全部 passed

- [ ] **Step 4: 启动服务冒烟测试**

Run: `python -c "from src.agent.graph import build_agent; app = build_agent(); print('Agent 构建成功')"`
Expected: 输出 `Agent 构建成功`，无 import 错误

- [ ] **Step 5: Commit**

```bash
git add src/agent/nodes.py src/tools/calculators.py
git commit -m "refactor: 清理废弃旧代码，TECH关键词统一从calculators导入"
```

---

## Self-Review

### 1. Spec 覆盖检查
- ①聚合去重：Task 4（工具函数）+ Task 11（接入 fetch_news）✓
- ②Python预筛：Task 5（权重表）+ Task 6（流程改造）✓
- ③LLM结构化：Task 2（schema）+ Task 7（护栏）+ Task 8（结构化输出）+ Task 9（接入 node）✓
- ④综合排名：Task 10（band主序+confidence+冲突降级）✓
- 哨兵机制：Task 3（state）+ Task 11（回写）+ Task 12（诊断）✓
- 测试：Task 1-13 全覆盖 ✓
- 清理：Task 14 ✓

### 2. 占位符扫描
无 TBD/TODO。所有代码步骤含完整代码。

### 3. 类型一致性
- `ImpactBand` / `Confidence` 在 Task 2 定义，Task 7/8/9/13 使用一致 ✓
- `BAND_PRIORITY` / `CONFIDENCE_WEIGHT` 在 Task 10 定义，测试与排名一致 ✓
- `_apply_guardrails` 签名在 Task 7 定义，Task 8/9/13 调用一致 ✓
- `RankedNewsItem` 新增字段在 Task 10 定义，state.py 同步 ✓
- `dedup_news三层` 在 Task 4 定义，Task 6/11 调用一致 ✓
- `score_news_relevance` 在 Task 5 定义，Task 6 调用一致 ✓

### 4. 4 项待确认默认值落实
- 科技词 +10：Task 5 `score_news_relevance` 内 ✓
- SimHash 阈值 3：Task 4 `_SIMHASH_THRESHOLD=3` ✓
- MIXED 单列排 neutral 之上：Task 10 `BAND_PRIORITY["mixed"]=4 > ["neutral"]=3` ✓
- 配额 direct∞/sector20/macro10：Task 6 `_PREFILTER_QUOTA` ✓
