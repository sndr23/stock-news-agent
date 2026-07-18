# A股资讯监测 Agent — 规则成熟化重构设计

> 日期：2026-07-17
> 状态：待评审
> 范围：资讯链路四环节规则升级（聚合去重 / Python 预筛 / LLM 结构化分析 / 综合排名）
> 方法：融合「方案一外科式移植」+「方案二范式重构」，借鉴 daily_stock_analysis、TradingAgents、Vibe-Trading 三家源码实现

---

## 1. 背景与目标

### 1.1 现状
项目链路本身成立：`fetch_news(多源聚合) → prefilter(Python预筛) → 条件路由 → llm_filter(LLM深度分析) → rank_news(综合排名)`。问题集中在各环节的**规则参数与判定逻辑稚嫩**：预筛靠黑名单+硬保底过粗、LLM 靠自由 JSON 不稳、排名靠拍脑袋系数。

### 1.2 目标
在不改变链路骨架的前提下，把四环节规则升级到与成熟项目对齐的水准：
- 预筛：从黑名单+保底 → 多因子权重打分表
- LLM：从自由 JSON → Pydantic 结构化 + 冲突护栏
- 排名：从单一连续分数 → 分级评级主序 + 连续分数次序 + confidence 加权
- 去重：从标题精确匹配 → URL + SimHash + 日期窗口三层

### 1.3 非目标（YAGNI）
- 不做多智能体辩论（场景是资讯排名，非个股决策，overkill）
- 不做 tool-calling（保持预取注入，防 LLM 伪造数据）
- 不做 UI / 多渠道推送 / 回测 / 持仓
- 不重构链路拓扑与 LangGraph 图结构

---

## 2. 设计原则

1. **最小改动**：链路、节点划分、State 字段保持不变；只替换节点内部规则与新增工具函数。
2. **风格对齐**：沿用 LangChain `@tool`、Pydantic、requests 直调 LLM 的现有风格；不引入新框架。
3. **分级评级优先**：成熟项目共性是"档位定大类、分数定细序"，本设计以此为排序核心范式。
4. **多层降级**：数据层→结构化层→LLM 层→规则层，每层失败有兜底，不中断流水线。
5. **保留差异化**：综合排名公式（可信度×重要度+聚类+方向折扣+科技加成）是本项目独有，不丢弃，降级为同档内次排序键。

---

## 3. 现状与问题

| 环节 | 文件 | 现状实现 | 核心问题 |
|---|---|---|---|
| ①聚合去重 | `src/tools/data_fetchers.py` | 4源并行 + `_dedup_by_title`精确匹配 + `_python_prefilter`聚类(2-gram Jaccard>0.35) | 仅标题精确去重，跨源同文不同标题漏网；无 URL 去重；无日期窗口防未来新闻 |
| ②Python预筛 | `src/agent/nodes.py` `_python_prefilter` | `NOISE_KEYWORDS`黑名单 + `TECH_HARDWARE_KEYWORDS`保底 + `calculate_prefilter_importance`多因子Sigmoid | 黑名单过粗漏噪；TECH保底一刀切；重要度Sigmoid压缩后区分度低 |
| ③LLM分析 | `src/agent/nodes.py` `llm_filter_node` | `ANALYSIS_PROMPT`自由JSON + `_call_llm_api` + `_safe_parse_json`多重容错 + 规则纠偏 | 自由JSON解析脆；方向判定常判中性；打分集中中庸；无结构化契约 |
| ④综合排名 | `src/tools/calculators.py` `rank_news` | 可信度×重要度(新闻0.15/0.70/0.15, 公告0.05/0.95, 信号0.10/0.90)+方向折扣+科技加成+国家级加成+ST降级 | 连续分数抗噪弱；小分差导致排名抖动；无 confidence 维度 |

---

## 4. 总体架构

链路与节点划分**不变**：

```
START → fetch_news → prefilter → [route_after_prefilter] → llm_filter → rank_news → END
                          │              │
                          │      go_to_llm/skip_to_rank
                          └──────────────┘
```

改动集中在节点内部规则与工具函数。新增一个 `src/schemas.py` 存放 Pydantic 模型。

---

## 5. 环节①：聚合去重升级

**文件**：`src/tools/data_fetchers.py`

### 5.1 保留
- 4 源并行（东财/财联社/新浪/同花顺）+ 公告 + 龙虎榜 + 业绩预告
- `_parallel_fetch` 每源独立超时、失败源隔离
- `_dedup_by_title` 标题精确匹配（作为去重第一层）
- 聚类热度 `cluster_weight`（2-gram Jaccard）作为后续排名补充信号

### 5.2 新增：URL 规范化去重
```python
# filepath: src/tools/data_fetchers.py
def _normalize_url(url: str) -> str:
    """URL 规范化：去 query/fragment，统一 host 小写，去末尾斜杠"""
    if not url:
        return ""
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))
```
- 在各 `_fetch_*` 返回的 news 字典里补 `url` 字段（akshare 部分接口有 URL，缺失则留空）
- 跨源聚合后按 `_normalize_url` 去重（URL 非空才参与）

### 5.3 新增：SimHash 近似去重（标题层）
借鉴 TradingAgents `get_global_news_yfinance` 的 `seen_titles` 思路，但升级为 SimHash 以捕获"同文不同标题"：
```python
# filepath: src/tools/data_fetchers.py
def _simhash(text: str, bits: int = 64) -> int:
    """字符级 3-gram SimHash。对中文短标题友好。"""
    grams = [text[i:i+3] for i in range(max(len(text)-2, 0))]
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
    return bin(a ^ b).count("1")

_SIMHASH_THRESHOLD = 3  # 海明距离 ≤3 视为近似重复，需实测微调
```
- 去重顺序：URL 精确 → 标题精确 → SimHash 近似（仅对标题）
- SimHash 阈值 `_SIMHASH_THRESHOLD=3` 标注为待实测校准项

### 5.4 新增：日期窗口防未来新闻
借鉴 TradingAgents `_in_news_window`：
```python
# filepath: src/tools/data_fetchers.py
def _in_news_window(published_at: str, look_back_days: int = 1) -> bool:
    """只保留 [今天-look_back_days, 今天] 的资讯，排除未来日期"""
    # 复用现有 _is_today 逻辑并扩展为窗口
```
- 替换现有 `_is_today` 为 `_in_news_window(look_back_days=1)`，保留"当日"语义但显式拒绝未来日期

### 5.5 新增：分级降级哨兵
借鉴 TradingAgents `route_to_vendor` 的核心/可选分级 + `NO_DATA_AVAILABLE` 哨兵：
```python
# filepath: src/tools/data_fetchers.py
NO_DATA_SENTINEL = "NO_DATA_AVAILABLE"  # 下游见到此值不得编造

# 核心源：新闻/公告/信号 —— 失败返回空列表 + 警告日志（现状保留）
# 哨兵用于：当全部核心源失败时，在 state 中标记 data_status=NO_DATA_SENTINEL
#          llm_filter/rank_news 见到哨兵则跳过 LLM 直接返回空结果并提示原因
```
- 在 `AgentState` 新增 `data_status: str` 字段（默认 "ok"），全源失败时置哨兵
- `route_after_prefilter` 见哨兵直接走 `skip_to_rank` 并在 rank 输出原因

---

## 6. 环节②：Python 预筛升级

**文件**：`src/agent/nodes.py`（`_python_prefilter`）+ `src/tools/calculators.py`（新增打分函数）

### 6.1 用 DSA 权重表替换 NOISE+TECH 保底
借鉴 `ZhuLinsen/daily_stock_analysis` 的 `_score_news_relevance`（源码逐字权重）：

```python
# filepath: src/tools/calculators.py

# 官方可信源 host 白名单（防 source label 伪装，以 URL host 为准）
_OFFICIAL_HOSTS = {
    "cninfo.com.cn", "sse.com", "sse.com.cn", "szse.cn",
    "hkexnews.hk", "sec.gov", "nasdaq.com", "nyse.com",
}
_OFFICIAL_LABELS = {"巨潮资讯", "上交所", "深交所", "港交所", "北交所"}

def _is_official_source(url: str = "", source: str = "") -> bool:
    host = _normalize_url(url).split("//")[-1].split("/")[0] if url else ""
    if host in _OFFICIAL_HOSTS:
        return True
    return any(lbl in source for lbl in _OFFICIAL_LABELS)

# 公司事件词（命中且 direct_signal>0 时 +12）
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
]

def score_news_relevance(item: dict, stock_code: str = "", stock_name: str = "") -> tuple[int, str]:
    """返回 (score 0-100, category: direct/sector/macro)
    
    权重表（借鉴 DSA _score_news_relevance）：
      代码命中 标题+55 / 摘要+34 / URL+18
      公司名命中-明确 标题+45 / 摘要+28
      公司名命中-歧义 标题+26 / 摘要+16
      事件词+12（条件：direct_signal>0）
      官方源+8
      板块词+6
      宏观词-12（条件：direct_signal==0）
      direct_signal>=38 → direct; 宏观命中且direct==0 → macro; 否则 sector
    """
    score = 0
    direct_signal = 0
    title = item.get("title", "")
    content = item.get("content", "")
    url = item.get("url", "")
    text = f"{title} {content}"

    # 代码命中（互斥，命中即 break）
    if stock_code:
        if stock_code in title:
            score += 55; direct_signal += 55
        elif stock_code in content:
            score += 34; direct_signal += 34
        elif stock_code in url:
            score += 18; direct_signal += 18

    # 公司名命中
    if stock_name:
        # 歧义英文短名（apple/meta 等）需事件词确认，此处 A 股以中文为主，默认按明确处理
        if stock_name in title:
            score += 45; direct_signal += 45
        elif stock_name in content:
            score += 28; direct_signal += 28

    # 事件词（条件性）
    if direct_signal > 0 and any(t in text for t in _COMPANY_EVENT_TERMS):
        score += 12; direct_signal += 12

    # 官方源
    if _is_official_source(url, item.get("source", "")):
        score += 8

    # 板块词
    if any(t in text for t in _SECTOR_NEWS_TERMS):
        score += 6

    # 宏观词（无 direct 时降权）
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

### 6.2 预筛流程改造
`_python_prefilter` 改为：
1. URL/SimHash 去重（环节①已做，这里消费结果）
2. 对每条资讯调 `score_news_relevance`（资讯流场景无固定 stock_code，主要靠事件词+板块词+官方源+宏观词打分；个股命中留作信号情报场景）
3. 按分类配额截断：`direct` 全留，`sector` 按分取 top，`macro` 按分取 top（配额可配，默认 direct∞/sector20/macro10，总上限 40）
4. 保留 `cluster_weight` 计算并附加到每条（作为排名补充信号）

### 6.3 删除
- `NOISE_KEYWORDS` 黑名单（被权重表中的宏观 -12 + 低分自然淘汰取代）
- `TECH_HARDWARE_KEYWORDS` 保底逻辑（科技关键词改为在权重表里作为板块词 +6，或在 `score_news_relevance` 里单独 +10 科技加分，待定——倾向后者，保留科技倾斜）

> 待定项：科技硬件词是否在 `score_news_relevance` 内单独 +10。倾向"是"，以保留你对科技板块的倾斜。评审时确认。

---

## 7. 环节③：LLM 结构化分析

**文件**：`src/agent/nodes.py`（`llm_filter_node`）+ 新增 `src/schemas.py`

### 7.1 新增 Pydantic Schema
借鉴 TradingAgents `SentimentReport`（6-band + 0-10 + confidence）+ DSA 结构化字段：

```python
# filepath: src/schemas.py
from pydantic import BaseModel, Field
from enum import Enum

class ImpactBand(str, Enum):
    BULLISH = "bullish"              # 强利好
    MILDLY_BULLISH = "mildly_bullish"
    NEUTRAL = "neutral"
    MIXED = "mixed"                  # 多空交织
    MILDLY_BEARISH = "mildly_bearish"
    BEARISH = "bearish"              # 强利空

class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class NewsAnalysisItem(BaseModel):
    title: str
    source: str = ""
    content: str = ""
    published_at: str = ""
    category: str = "news"
    market_impact_score: float = Field(ge=0.0, le=10.0, description="0最无影响, 10极重大")
    impact_band: ImpactBand
    confidence: Confidence
    affected_sectors: list[str] = []
    affected_stocks: list[str] = []
    impact_reason: str = ""
    sentiment: str = ""  # 与 impact_band 对齐

class NewsAnalysisBatch(BaseModel):
    filtered_news: list[NewsAnalysisItem]
    removed_count: int = 0
    analysis_summary: str = ""
```

**band↔score 一致性指南**（写入 Field description / 提示词，借 TradingAgents）：
- BULLISH 6.5–10
- MILDLY_BULLISH 5.5–6.4
- NEUTRAL / MIXED 4.5–5.5
- MILDLY_BEARISH 3.5–4.4
- BEARISH 0–3.4

### 7.2 结构化输出 + 降级
借鉴 TradingAgents `bind_structured` + `invoke_structured_or_freetext`：
```python
# filepath: src/agent/nodes.py
from langchain_openai import ChatOpenAI  # 或现有 requests 直调的封装

def _llm_analyze_batch_structured(batch: list) -> list:
    """结构化输出 + 自由文本降级"""
    llm = _build_llm()  # 复用现有 OPENROUTER 配置
    prompt = _build_analysis_prompt(batch)  # 预取注入，整块塞提示词
    
    # 方式A：LangChain with_structured_output（OpenAI 兼容 provider 多数支持）
    try:
        structured_llm = llm.with_structured_output(NewsAnalysisBatch)
        result = structured_llm.invoke(prompt)
        if result and result.filtered_news is not None:
            return _apply_guardrails(result.filtered_news)
    except Exception as e:
        logger.warning(f"结构化输出失败，降级自由文本: {e}")
    
    # 方式B：降级到现有 _call_llm_api + _safe_parse_json
    content = _call_llm_api(SYSTEM_MSG, prompt, timeout=90, max_retries=2)
    parsed = _safe_parse_json(content)
    items = [NewsAnalysisItem(**item) for item in parsed.get("filtered_news", [])]
    return _apply_guardrails(items)
```

### 7.3 预取注入（保留，非 tool-calling）
借鉴 TradingAgents v0.2.5 教训：提示词要求的数据若让 LLM 自己调工具取，弱模型会伪造。**保持现状**——Python 侧把批次资讯整块注入提示词，LLM 不调工具。

`ANALYSIS_PROMPT` 改造要点（借鉴 Vibe-Trading "Required outputs 编号契约"）：
```
你是资深A股资讯分析师。对以下资讯逐条分析，输出结构化 JSON。

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
4. affected_sectors: 必填，涉及板块（半导体/CPO/PCB/算力/新能源/...）
5. affected_stocks: 明确提及的个股
6. impact_reason: 一句话影响逻辑

## 规则
- band 与 score 必须一致（见上区间），冲突时以 score 为准调整 band
- 含明确多空信号的严禁判 neutral/mixed
- 科技板块资讯以"对科技板块的影响"判定方向
- 噪音（庆典/八卦/软文）不输出到 filtered_news，计入 removed_count
```

### 7.4 冲突护栏
借鉴 DSA `score_action_conflicts_without_guardrail`：
```python
# filepath: src/agent/nodes.py
BAND_SCORE_RANGE = {
    ImpactBand.BULLISH: (6.5, 10),
    ImpactBand.MILDLY_BULLISH: (5.5, 6.4),
    ImpactBand.NEUTRAL: (4.5, 5.5),
    ImpactBand.MIXED: (4.5, 5.5),
    ImpactBand.MILDLY_BEARISH: (3.5, 4.4),
    ImpactBand.BEARISH: (0, 3.4),
}

def _apply_guardrails(items: list[NewsAnalysisItem]) -> list[NewsAnalysisItem]:
    """band 与 score 冲突时，按 score 强制校正 band"""
    for item in items:
        lo, hi = BAND_SCORE_RANGE[item.impact_band]
        if not (lo <= item.market_impact_score <= hi):
            # 按score重新分档
            item.impact_band = _band_from_score(item.market_impact_score)
            item.sentiment = item.impact_band.value
    return items

def _band_from_score(score: float) -> ImpactBand:
    if score >= 6.5: return ImpactBand.BULLISH
    if score >= 5.5: return ImpactBand.MILDLY_BULLISH
    if score >= 4.5: return ImpactBand.NEUTRAL  # 或 MIXED，默认 NEUTRAL
    if score >= 3.5: return ImpactBand.MILDLY_BEARISH
    return ImpactBand.BEARISH
```

### 7.5 规则纠偏兜底（保留）
LLM API 完全失败时，保留现有 `predict_direction_by_rules` / `infer_sectors_by_rules`，并补默认 `impact_band=NEUTRAL / score=3.0 / confidence=low`。

---

## 8. 环节④：综合排名升级

**文件**：`src/tools/calculators.py`（`rank_news`）

### 8.1 排序键改造
从单一 `total_score` 改为三级排序键：

```python
# filepath: src/tools/calculators.py
BAND_PRIORITY = {
    "bullish": 6, "mildly_bullish": 5,
    "neutral": 3, "mixed": 4,  # mixed 排在 neutral 之上（有信号优于无信号）
    "mildly_bearish": 2, "bearish": 1,
}

CONFIDENCE_WEIGHT = {
    "high": 1.0, "medium": 0.85, "low": 0.7,  # 用户已确认
}

def rank_news(news_list: list) -> list:
    ranked = []
    for news in news_list:
        # ---- 保留现有连续分数计算（可信度×重要度+聚类+方向折扣+科技加成）----
        total = _calc_continuous_score(news)  # 原rank_news内的计算逻辑抽函数
        
        # ---- 新增：confidence 加权 ----
        conf = news.get("confidence", "medium")
        total = round(total * CONFIDENCE_WEIGHT.get(conf, 0.85), 4)
        
        # ---- 新增：band 与方向冲突降级 ----
        band = news.get("impact_band", "neutral")
        direction = news.get("impact_direction", "neutral")
        if _band_direction_conflict(band, direction):
            band = _downgrade_band(band)  # 降一档
        
        ranked.append({
            ...news,
            "total_score": total,
            "impact_band": band,
            "band_priority": BAND_PRIORITY.get(band, 3),
        })
    
    # 三级排序：band优先级 → 连续分数 → 时间因子
    ranked.sort(key=lambda x: (x["band_priority"], x["total_score"], x["time_factor"]), reverse=True)
    return ranked
```

### 8.2 连续分数公式（保留，抽函数）
现有 `rank_news` 内的 `total_base`（分类权重）+ `sentiment_factor`（方向折扣）+ 科技加成 + 国家级加成 + ST 降级，整体抽为 `_calc_continuous_score(news) -> float`，逻辑不变。

### 8.3 band 与方向冲突判定
```python
def _band_direction_conflict(band: str, direction: str) -> bool:
    """如 impact_band=bullish 但 impact_direction=bearish 视为冲突"""
    bullish_bands = {"bullish", "mildly_bullish"}
    bearish_bands = {"bearish", "mildly_bearish"}
    if band in bullish_bands and direction == "bearish":
        return True
    if band in bearish_bands and direction == "bullish":
        return True
    return False

def _downgrade_band(band: str) -> str:
    order = ["bullish", "mildly_bullish", "mixed", "neutral", "mildly_bearish", "bearish"]
    idx = order.index(band) if band in order else 3
    return order[min(idx + 1, len(order) - 1)]
```

### 8.4 `RankedNewsItem` TypedDict 扩展
新增字段：`impact_band: str`、`band_priority: int`、`confidence: str`。

---

## 9. 错误处理与降级（四层）

| 层 | 机制 | 文件 | 行为 |
|---|---|---|---|
| 数据层 | 核心源失败→空列表+日志；全源失败→`NO_DATA_SENTINEL` 哨兵 | data_fetchers.py | 不中断，下游见哨兵跳过 LLM |
| 预筛层 | 权重表打分失败→默认 score=10,category=sector | calculators.py | 不中断 |
| 结构化层 | `with_structured_output` 失败→自由文本+`_safe_parse_json` | nodes.py | 降级解析 |
| LLM 层 | API 失败→规则兜底+默认 band/score/confidence | nodes.py | 降级标注 |

---

## 10. 测试策略

| 测试文件 | 覆盖点 |
|---|---|
| `tests/test_dedup.py` | URL 规范化、SimHash 近似、日期窗口防未来 |
| `tests/test_prefilter.py` | `score_news_relevance` 各权重档命中、direct/sector/macro 分类、官方源白名单 |
| `tests/test_llm_filter.py` | Pydantic schema 解析、`_apply_guardrails` 冲突校正、结构化降级、规则兜底 |
| `tests/test_rank.py` | band 主序、confidence 加权、band-方向冲突降级、连续分数回归 |
| 现有 `calculators.py __main__` | 保留作回归基线 |

测试数据：构造覆盖 6-band × 3-confidence × 3-category 的最小用例集。

---

## 11. 文件改动清单

| 文件 | 改动 |
|---|---|
| `src/tools/data_fetchers.py` | 新增 `_normalize_url`/`_simhash`/`_hamming`/`_in_news_window`；去重升级；`NO_DATA_SENTINEL`；`data_status` 回写 |
| `src/tools/calculators.py` | 新增 `score_news_relevance`/`_is_official_source`/`_OFFICIAL_HOSTS`/`BAND_PRIORITY`/`CONFIDENCE_WEIGHT`/`_calc_continuous_score`/`_band_direction_conflict`/`_downgrade_band`；`rank_news` 改三级排序 |
| `src/agent/nodes.py` | `_python_prefilter` 用新权重表；`llm_filter_node` 用 Pydantic schema+`with_structured_output`+`_apply_guardrails`；`ANALYSIS_PROMPT` 改输出契约 |
| `src/schemas.py` | 新增：`ImpactBand`/`Confidence`/`NewsAnalysisItem`/`NewsAnalysisBatch` |
| `src/agent/state.py` | `AgentState` 新增 `data_status: str` |

---

## 12. 不改动部分（YAGNI）

- LangGraph 图结构、节点划分、`route_after_prefilter` 路由逻辑
- `_call_llm_api` 的 requests 直调方式（仅新增结构化输出路径）
- `_parallel_fetch` 并行框架
- `index.html` 前端、`launcher.py`、`api/main.py` API
- 不引入多智能体、tool-calling、回测、持仓、推送

---

## 13. 风险与取舍

| 风险 | 影响 | 缓解 |
|---|---|---|
| `with_structured_output` 对 OpenRouter/OpenAI 兼容 provider 支持不稳 | 结构化输出可能频繁降级 | 降级路径已内置（自由文本+`_safe_parse_json`）；测试期统计降级率 |
| SimHash 对中文短标题效果未验证 | 近似去重可能漏/误 | `_SIMHASH_THRESHOLD=3` 标注待实测；提供开关可回退到标题精确匹配 |
| 6-band 中 MIXED 与 NEUTRAL LLM 区分不稳 | 排序抖动 | 提示词强约束 + 冲突护栏按 score 兜底；MIXED 默认归 NEUTRAL |
| DSA 权重表为"个股资讯"设计，资讯流场景无固定 stock_code | direct_signal 多数为 0，主要靠事件词+板块词 | 事件词 +12 仍可触发；科技词单独 +10 保留倾斜（待评审确认） |
| 删除 NOISE 黑名单后噪音可能反弹 | 预筛漏噪 | 宏观 -12 + 低分自然淘汰 + LLM 层 `removed_count` 兜底；回归测试监控 |

---

## 14. 待评审确认项

1. **科技硬件词在 `score_news_relevance` 内是否单独 +10**（保留科技倾斜）？倾向"是"。
2. **`_SIMHASH_THRESHOLD` 初始值 3** 是否接受作为待实测起点？
3. **MIXED 默认归 NEUTRAL** 还是单列？倾向"单列但排序在 neutral 之上"。
4. **预筛分类配额** direct∞/sector20/macro10 是否合理？
