# filepath: src/agent/nodes.py
"""
A股资讯监测 Agent 节点实现
3个节点: fetch_news -> filter_noise -> rank_news

两阶段过滤方案 (解决当日资讯量可能数百上千条的问题):
  Stage 1 - Python预过滤 (毫秒级, 处理数百条->约40条):
    - 关键词去噪 (庆典/年会/八卦等)
    - 标题去重
    - 重要度初筛 (取top 40)
  Stage 2 - LLM分析 (每批10条约25秒):
    - 剩余噪音识别
    - 利好/利空方向标注
    - 影响板块/个股提取
"""
import json
import socket
import time
import logging
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

from src.config import OPENROUTER_API_KEY, OPENROUTER_MODEL_NAME, OPENROUTER_BASE_URL
from src.tools.data_fetchers import get_stock_news, get_announcements, get_market_signals, dedup_news三层
from src.tools.calculators import rank_news, predict_direction_by_rules, infer_sectors_by_rules, score_news_relevance, TECH_HARDWARE_KEYWORDS
from src.agent.state import AgentState, NO_DATA_SENTINEL
from src.schemas import ImpactBand, Confidence, NewsAnalysisItem, NewsAnalysisBatch


def _call_llm_api(system_prompt: str, user_prompt: str, timeout: int = 90, max_retries: int = 2) -> str:
    """直接用 requests 调用 LLM API

    关键: trust_env=False 禁止 requests 读取系统代理设置(Windows注册表/env vars),
    避免代理服务未运行时导致 ProxyError/ConnectionRefused

    Args:
        system_prompt: 系统提示词
        user_prompt: 用户提示词
        timeout: 单次请求超时秒数
        max_retries: 最大重试次数

    Returns:
        LLM 返回的文本内容
    """
    import requests

    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": OPENROUTER_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 16384
    }

    last_error = None
    for attempt in range(max_retries + 1):
        session = requests.Session()
        session.trust_env = False
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if content:  # 非空内容才返回
                return content
            else:
                last_error = f"第{attempt+1}次返回空内容"
                logger.warning(last_error)
        except Exception as e:
            last_error = str(e)
            logger.warning(f"第{attempt+1}次调用失败: {e}")
        finally:
            session.close()

        if attempt < max_retries:
            # 指数退避: 2s, 4s
            wait_time = 2 ** (attempt + 1)
            logger.info(f"等待 {wait_time}s 后重试...")
            time.sleep(wait_time)

    raise Exception(f"LLM API 调用失败，已重试 {max_retries} 次: {last_error}")


# ============================================================
# 节点1: fetch_news - 抓取当日全部资讯
# ============================================================

def fetch_news_node(state: AgentState) -> dict:
    """抓取当日全部A股资讯（多源新闻 + 公告）

    数据源内部已并行获取并按当日过滤+去重
    """
    data_mode = state.get("data_mode", "live")

    raw_news = get_stock_news.invoke({"data_mode": data_mode})
    announcements = get_announcements.invoke({"data_mode": data_mode})
    market_signals = get_market_signals.invoke({"data_mode": data_mode})

    # 将公告归一化为新闻格式
    ann_as_news = []
    for ann in announcements:
        ann_as_news.append({
            "title": ann.get("title", ""),
            "source": f"{ann.get('name', '')}公告" if ann.get("name") else "交易所公告",
            "content": ann.get("content", ""),
            "published_at": ann.get("published_at", ""),
            "category": "announcement",
            "sentiment": "neutral",
            "name": ann.get("name", ""),
            "code": ann.get("code", "")
        })

    all_news = raw_news + ann_as_news + market_signals

    # 三层去重（URL/标题/SimHash）
    before_dedup = len(all_news)
    all_news = dedup_news三层(all_news)
    logger.info(f"[fetch_news] 三层去重: {before_dedup} -> {len(all_news)}条")

    # 哨兵：全部核心源失败
    data_status = "ok"
    if not all_news:
        data_status = NO_DATA_SENTINEL
        logger.warning("[fetch_news] 全部数据源失败，置 NO_DATA 哨兵")

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


# ============================================================
# Stage 1: Python预过滤 (毫秒级)
# ============================================================

def _calc_similarity(text1: str, text2: str) -> float:
    # 简单的2-gram字符级别Jaccard相似度
    set1 = set([text1[i:i+2] for i in range(len(text1)-1)])
    set2 = set([text2[i:i+2] for i in range(len(text2)-1)])
    if not set1 or not set2:
        return 0.0
    return len(set1 & set2) / len(set1 | set2)


_PREFILTER_QUOTA = {"direct": None, "sector": 30, "macro": 15}
_PREFILTER_TOTAL_LIMIT = 60


def _python_prefilter(news_list: list, top_n: int = _PREFILTER_TOTAL_LIMIT) -> tuple:
    """Python 预筛：权重表打分 + 分类配额截断 + 聚类热度

    流程：
      1. 三层去重（URL/标题/SimHash）
      2. 对每条调 score_news_relevance 打分分类
      3. 按分类配额截断：direct全留, sector取top20, macro取top10
      4. 计算聚类热度 cluster_weight

    注：噪音过滤交由权重表负责——零关联度且 sector 类的条目视为纯噪音丢弃，
        不再使用 nodes.py 内的 NOISE_KEYWORDS 黑名单（已删除）。
    """
    deduped = dedup_news三层(news_list)
    dup_count = len(news_list) - len(deduped)

    for news in deduped:
        score, category = score_news_relevance(news)
        news["_prefilter_score"] = score
        news["_prefilter_category"] = category

    # 权重表初筛：丢弃"零关联度且 sector 类"的纯噪音条目
    # （macro 类即使被权重表罚分至 0 也保留，因其类别本身即宏观信号）
    scored = [n for n in deduped if n["_prefilter_score"] > 0 or n["_prefilter_category"] != "sector"]

    buckets = {"direct": [], "sector": [], "macro": []}
    for news in scored:
        cat = news["_prefilter_category"]
        buckets[cat].append(news)

    for cat in buckets:
        buckets[cat].sort(key=lambda x: x["_prefilter_score"], reverse=True)

    kept = []
    for cat, quota in _PREFILTER_QUOTA.items():
        if quota is not None:
            kept.extend(buckets[cat][:quota])
        else:
            kept.extend(buckets[cat])

    if len(kept) > top_n:
        kept.sort(key=lambda x: x["_prefilter_score"], reverse=True)
        kept = kept[:top_n]

    for i, news1 in enumerate(kept):
        cluster_size = 1
        title1 = news1.get("title", "")
        for j, news2 in enumerate(kept):
            if i != j:
                title2 = news2.get("title", "")
                if _calc_similarity(title1, title2) > 0.35:
                    cluster_size += 1
        news1["cluster_weight"] = min(cluster_size - 1, 10)

    for news in kept:
        news.pop("_prefilter_score", None)
        news.pop("_prefilter_category", None)
        # 清理所有以 _ 开头的临时字段，防止泄漏到 LLM 输入
        for key in list(news.keys()):
            if key.startswith("_"):
                news.pop(key, None)

    total_removed = dup_count + (len(deduped) - len(kept))
    return kept, total_removed


# ============================================================
# Stage 2: LLM分析 (标签化)
# ============================================================

ANALYSIS_PROMPT = """你是拥有10年A股投研经验的资深资讯分析师。请对以下资讯逐条深度分析，输出结构化 JSON。

## 资讯列表（共{n}条）
{news_list}

## 分析框架（请按以下5步逐条思考，并在 analysis_chain 中记录推理过程）

### 第1步：事件识别
- 事件类型：业绩/政策/并购/技术突破/处罚/融资/减持/...
- 核心事实：提取关键数据和主体
- 信息完整性：是否有具体金额/比例/时间？

### 第2步：影响范围判断（最关键步骤）
按以下顺序逐层判断，取最高层级作为 influence_scope：

1. 市场级(market)：影响整个A股市场/大盘
   - 央行/财政部/证监会/国务院的全面性政策（降息降准、注册制改革、印花税调整）
   - 重大地缘政治事件（影响全市场情绪）
   - 跨3个以上板块的系统性事件

2. 板块级(sector)：影响整个行业/板块
   - 行业政策（如半导体补贴、新能源规划）→ 整个板块
   - 龙头股重大事件 → 带动板块情绪和估值
     * 龙头判断标准：市值前列、板块风向标、机构持仓集中
     * 典型龙头：中际旭创/新易盛(CPO) | 宁德时代(新能源) | 贵州茅台(白酒) | 招商银行(银行) | 中芯国际(半导体) | 工业富联(算力)
   - 供应链传导（如上游涨价→下游成本上升→整个链条）
   - 板块性技术趋势（如CPO技术路线确立）

3. 个股级(stock)：仅影响个股本身
   - 非龙头股的普通公告（业绩波动、常规经营）
   - 无板块联动效应的个股事件
   - 注意：即使是利好，如果只是个股层面且非龙头，不应判为 sector

### 第3步：方向判断
- 对受影响对象是利好还是利空？
- 科技板块资讯以"对科技板块的影响"判定方向
- 含明确多空信号的严禁判 neutral/mixed
- 同时含利好利空判 mixed

### 第4步：强度评估（0-10分）
- 0-2: 几乎无影响（常规播报、无关资讯）
- 3-4: 弱影响（个股普通公告、非龙头常规经营）
- 5-6: 中等影响（板块政策、龙头常规事件）
- 7-8: 强影响（重大政策、龙头重大事件、板块性突破）
- 9-10: 极重大（可能改变市场走势的里程碑事件）

### 第5步：置信度评估
- high: 多源报道/有具体数据/官方公告
- medium: 单一来源但有事件细节
- low: 内容<50字/信息不足/市场传闻

## analysis_chain 写法
用箭头连接5步结论，简明记录推理过程（一条资讯一行）：
- "CPO龙头中际旭创业绩预增80%→光模块板块龙头→带动板块估值→强利好→高置信"
- "央行降准0.5%→全市场流动性宽松→利好大盘→强利好→高置信"
- "某小盘股获补贴200万→仅个股影响→弱利好→低置信"

## 输出契约（每条必须包含全部字段）
1. market_impact_score: 0-10（0无影响/10极重大）
2. impact_band: 6档之一（与score区间一致）
   - bullish(强利好, 6.5-10): 业绩预增/大额中标/政策扶持/增持回购/技术突破
   - mildly_bullish(弱利好, 5.5-6.4): 普通经营利好
   - neutral(中性, 4.5-5.5): 无明显多空的常规播报
   - mixed(多空交织, 4.5-5.5): 同时含利好利空
   - mildly_bearish(弱利空, 3.5-4.4): 普通经营利空
   - bearish(强利空, 0-3.4): 立案/退市/爆雷/违约/重大处罚
3. confidence: high/medium/low
4. affected_sectors: 必填，涉及板块（半导体/CPO/PCB/算力/新能源/医药/银行/...）
5. affected_stocks: 明确提及的个股
6. impact_reason: 一句话影响逻辑
7. influence_scope: market/sector/stock（按第2步判断）
8. analysis_chain: 5步推理链（箭头连接，简明记录思考过程）

## 规则
- band 与 score 必须一致（见上区间），冲突时以 score 为准调整 band
- 含明确多空信号的严禁判 neutral/mixed
- 科技板块资讯以"对科技板块的影响"判定方向
- 噪音不输出到 filtered_news，计入 removed_count。噪音定义：
  - 庆典/八卦/软文/公关稿
  - 与A股无直接关联的纯国际资讯（如海外天气、非涉华国际事件）
  - 纯宏观经济评论（无具体板块/个股影响逻辑）
  - 重复报道同一事件（只保留信息量最大的一条）

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
      "impact_reason": "半导体国产替代加速，利好板块龙头",
      "influence_scope": "sector",
      "analysis_chain": "中芯国际获大额订单→半导体龙头→带动国产替代板块→强利好→高置信"
    }}
  ],
  "removed_count": 0,
  "analysis_summary": "本次分析简要摘要"
}}
```

注意:
- filtered_news 中每条必须保留原始字段并新增上述字段
- affected_sectors 必须尽力提取，仅当完全不涉及行业板块时才设为空数组
- analysis_chain 必须填写，记录5步推理过程
"""


def _repair_json(text: str) -> str:
    """尝试修复LLM返回的JSON格式问题"""
    import re
    # 替换中文引号
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    # 替换中文冒号
    text = text.replace('\uff1a', ':')
    # 替换中文逗号
    text = text.replace('\uff0c', ',')
    # 替换中文句号
    text = text.replace('\u3002', '.')
    # 替换中文顿号
    text = text.replace('\u3001', ',')
    # 替换不可见字符
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    # 替换未转义的换行符
    text = re.sub(r'(?<=": ")[^\n"]*(?=\n)', lambda m: m.group(0).replace('\n', '\\n').replace('\r', '\\r'), text, flags=re.DOTALL)
    return text


def _safe_parse_json(content: str) -> dict:
    """安全解析LLM返回的JSON, 多重容错"""
    if not content or not content.strip():
        return {"filtered_news": [], "removed_count": 0}

    # 预处理: 清理可能的乱码
    import re
    cleaned = content
    cleaned = cleaned.replace('\u0000', '')  # 移除null字符
    # 先提取代码块（避免代码块标记被破坏）
    if "```json" in cleaned:
        cleaned = cleaned.split("```json")[1].split("```")[0]
    elif "```" in cleaned:
        cleaned = cleaned.split("```")[1].split("```")[0]
    # 先修复中文标点（中文引号/冒号/逗号等），再做不可见字符清理
    cleaned = _repair_json(cleaned)
    # 清理不可见字符，但保留中文引号/标点（已转换为ASCII）和中文汉字
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', cleaned)

    cleaned = cleaned.strip()

    if not cleaned:
        return {"filtered_news": [], "removed_count": 0}

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    try:
        repaired = _repair_json(cleaned)
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # 尝试提取 filtered_news 数组
    import re
    match = re.search(r'\{[^{}]*"(?:filtered_news|removed_count)"[^{}]*\}', cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    news_match = re.search(r'"filtered_news"\s*:\s*\[', cleaned)
    if news_match:
        start = news_match.end() - 1
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == '[':
                depth += 1
            elif cleaned[i] == ']':
                depth -= 1
                if depth == 0:
                    try:
                        news_array = json.loads(cleaned[start:i+1])
                        return {"filtered_news": news_array, "removed_count": 0}
                    except json.JSONDecodeError:
                        break

    # 最终降级: 返回空结果而不是抛出异常
    logger.warning(f"JSON解析最终失败，降级返回空结果: {cleaned[:100]}...")
    return {"filtered_news": [], "removed_count": 0}


# ============================================================
# 冲突护栏（借鉴 DSA score_action_conflicts_without_guardrail）
# ============================================================

BAND_SCORE_RANGE = {
    ImpactBand.BULLISH: (6.5, 10.0),
    ImpactBand.MILDLY_BULLISH: (5.5, 6.499),
    ImpactBand.NEUTRAL: (4.5, 5.499),
    ImpactBand.MIXED: (4.5, 5.499),
    ImpactBand.MILDLY_BEARISH: (3.5, 4.499),
    ImpactBand.BEARISH: (0.0, 3.499),
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
            try:
                score = float(item.get("market_impact_score", 3.0))
            except (ValueError, TypeError):
                score = 3.0
            if not (lo <= score <= hi):
                band = _band_from_score(score)
            item["impact_band"] = band.value
            item["sentiment"] = band.value
            item["impact_direction"] = _band_to_direction(band)
    return items


def _build_llm():
    """构建 LangChain ChatOpenAI（复用 OpenRouter 配置）

    max_tokens=16384: 推理模型(agnes-2.0-flash)的 reasoning_tokens 会占用大量额度，
    4096 时几乎全部用于推理导致 JSON 输出被截断。提升到 16384 确保推理+文本输出都有空间。
    reasoning_effort="low": 仅对推理模型(agnes/o1/deepseek-r1)生效，减少推理开销。
    """
    from langchain_openai import ChatOpenAI
    # 仅推理模型传 reasoning_effort（Gemini 等非推理模型会拒绝该参数）
    reasoning_models = ("agnes", "o1", "o3", "deepseek-r1", "deepseek-reasoner")
    extra_body = {}
    if any(m in OPENROUTER_MODEL_NAME.lower() for m in reasoning_models):
        extra_body["reasoning_effort"] = "low"  # 限流接口需快速返回，避免524超时
    return ChatOpenAI(
        model=OPENROUTER_MODEL_NAME,
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
        temperature=0.1,  # 结构化输出场景降低温度提升一致性
        max_tokens=16384,
        extra_body=extra_body,
        timeout=90,  # 非推理模型响应快
    )


def _build_analysis_prompt(news_batch: list) -> str:
    """构建分析提示词（预取注入，整块塞提示词）"""
    truncated_batch = []
    for n in news_batch:
        item = dict(n)
        content = item.get("content", "")
        if len(content) > 300:
            item["content"] = content[:300] + "..."
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
        return batch


# ============================================================
# 节点2: prefilter - Python预过滤
# ============================================================

def prefilter_node(state: AgentState) -> dict:
    """Stage 1 (Python) 预过滤: 关键词去噪 + 去重 + 重要度初筛 -> top 30
    
    结果写入 prefiltered_news
    """
    import logging
    logger = logging.getLogger(__name__)

    raw_news = state.get("raw_news", [])
    prefiltered, py_removed = _python_prefilter(raw_news, top_n=_PREFILTER_TOTAL_LIMIT)

    logger.info(f"[prefilter] Python预过滤: 原始{len(raw_news)}条 -> 保留{len(prefiltered)}条, 去除{py_removed}条")

    return {
        "prefiltered_news": prefiltered,
        "messages": [
            AIMessage(content=(
                f"[prefilter] Python预筛完成：从{len(raw_news)}条中筛选出{len(prefiltered)}条重要资讯，"
                f"过滤噪音/重复共{py_removed}条。"
            ))
        ]
    }


# ============================================================
# 条件路由决策
# ============================================================

# 重磅信号关键词 (触发 LLM 深度分析的阈值)
HIGH_SIGNAL_KEYWORDS = [
    # 重大事件
    "退市", "立案调查", "重大违法", "破产", "业绩暴雷", "巨额亏损",
    "债务违约", "重大重组", "借壳", "并购", "涨停", "跌停",
    "监管处罚", "业绩超预期", "业绩预增", "业绩预减", "爆雷",
    "ST", "*ST",
    # 信号情报关键词
    "龙虎榜", "机构净买入", "业绩预告",
    # 政策
    "降准", "降息", "加息", "印花税", "注册制",
    "产业政策", "补贴", "减税", "政策利好",
    # 中等信号
    "北向资金", "回购", "增持", "减持", "分红", "股权激励",
    "IPO", "定增", "可转债",
]


def _has_high_signal(news_list: list) -> bool:
    """检测资讯列表中是否存在任何重磅信号关键词 (含科技硬件)"""
    for news in news_list:
        title = news.get("title", "")
        content = news.get("content", "")
        name = news.get("name", "")
        clean_title = title.replace(name, "") if name else title
        clean_content = content.replace(name, "") if name else content
        clean_text = f"{clean_title} {clean_content}"
        
        if any(kw in clean_text for kw in TECH_HARDWARE_KEYWORDS):
            return True
        if any(kw in clean_text for kw in HIGH_SIGNAL_KEYWORDS):
            return True
    return False


def route_after_prefilter(state: AgentState) -> str:
    """条件路由：根据预筛结果，智能决定是否走 LLM 分析
    
    规则：
    - 哨兵优先：数据源全失败，直接跳到排名；
    - 如果 prefiltered 为空，直达排名；
    - 如果预筛列表中包含重磅信号（科技硬件/政策/ST等），必须走 LLM 深度分析；
    - 如果预筛列表很少（<=3条）且无重磅信号，可跳过 LLM 直接排名节省时间；
    - 否则默认走 LLM 分析。
    """
    # 哨兵：数据源全失败，跳过 LLM
    if state.get("data_status") == NO_DATA_SENTINEL:
        return "skip_to_rank"

    prefiltered = state.get("prefiltered_news", [])
    if not prefiltered:
        return "skip_to_rank"

    # 检查是否有重磅信号
    if _has_high_signal(prefiltered):
        return "go_to_llm"

    # 如果数量很少且无重磅信号，跳过 LLM 节省时间
    if len(prefiltered) <= 3:
        return "skip_to_rank"

    # 默认走 LLM
    return "go_to_llm"


# ============================================================
# 节点3: llm_filter - 条目级分流 LLM 标签化
# ============================================================

def llm_filter_node(state: AgentState) -> dict:
    """Stage 2 (LLM): 调用 LLM 进行深度去噪和标签化。"""
    import logging
    import socket
    logger = logging.getLogger(__name__)

    prefiltered = state.get("prefiltered_news", [])
    if not prefiltered:
        return {
            "filtered_news": [],
            "messages": [AIMessage(content="[llm_filter] 无有效资讯需分析")]
        }

    logger.info(f"[llm_filter] 预筛{len(prefiltered)}条，准备全部进行深度分析")

    # 并发批量调用 LLM
    all_llm_results = []
    total_llm_removed = 0
    batch_errors = 0
    error_details = []

    BATCH_SIZE = 15
    batches = [prefiltered[i:i + BATCH_SIZE] for i in range(0, len(prefiltered), BATCH_SIZE)]

    old_socket_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(None)

    try:
        # 串行调用（qwqtao 等接口限流1分钟3次，并发会导致大量429）
        # 批次间间隔 21 秒，确保 1 分钟内不超过 3 次请求
        results_map = {}
        for idx, batch in enumerate(batches):
            try:
                results_map[idx] = _llm_analyze_batch_structured(batch)
                logger.info(f"LLM 批次 {idx} 分析完成")
            except Exception as e:
                results_map[idx] = None
                batch_errors += 1
                error_details.append(f"批次{idx}: {str(e)[:80]}")
                logger.warning(f"LLM 批次 {idx} 失败: {e}")
            # 批次间限流间隔（最后一批不需要等）
            if idx < len(batches) - 1:
                logger.info(f"限流等待 21s（qwqtao: 1分钟最多3次请求）...")
                time.sleep(21)

        # 合并 LLM 结果（兼容 NewsAnalysisItem 对象与 dict）
        for idx in range(len(batches)):
            batch = batches[idx]
            filtered_batch = results_map.get(idx)
            if filtered_batch:
                for item in filtered_batch:
                    if isinstance(item, NewsAnalysisItem):
                        all_llm_results.append(item.model_dump())
                    elif isinstance(item, dict):
                        all_llm_results.append(item)
                    else:
                        all_llm_results.append(item)
            else:
                # 失败降级处理
                for n in batch:
                    all_llm_results.append(n)

    except Exception as e:
        logger.error(f"[llm_filter] LLM 并发流异常: {e}", exc_info=True)
        # 全部降级
        all_llm_results = prefiltered
    finally:
        socket.setdefaulttimeout(old_socket_timeout)

    # 3. 结果合并: 使用 LLM 结果中的 impact 字段更新原始 news 对象, 保留原始所有的属性(解决丢失问题)
    final_filtered = []
    # 用 (title, published_at) 复合 key 避免同标题碰撞
    llm_results_by_key = {}
    for n in all_llm_results:
        key = (n.get("title", "").strip(), str(n.get("published_at", "")))
        llm_results_by_key[key] = n
    # 同时保留 title-only 索引作为兜底（LLM 可能截断 published_at）
    llm_results_by_title = {n.get("title", "").strip(): n for n in all_llm_results}

    for news in prefiltered:
        title = news.get("title", "").strip()
        pub = str(news.get("published_at", ""))
        # 优先复合 key 匹配，其次 title 匹配
        llm_res = llm_results_by_key.get((title, pub)) or llm_results_by_title.get(title)
        if llm_res:
            # LLM 没有认为是噪音，合并字段

            # 信号情报: 方向已由交易所官方数据锁定, 不被 LLM 覆盖
            if news.get("category") == "signal":
                news["market_impact_score"] = llm_res.get("market_impact_score", news.get("market_impact_score", 5.0))
                news["affected_sectors"] = llm_res.get("affected_sectors", news.get("affected_sectors", []))
                news["affected_stocks"] = llm_res.get("affected_stocks", news.get("affected_stocks", []))
                if llm_res.get("impact_reason"):
                    news["impact_reason"] = llm_res["impact_reason"]
                news["influence_scope"] = llm_res.get("influence_scope", "stock")
                news["analysis_chain"] = llm_res.get("analysis_chain", "")
                # 根据 direction 同步 impact_band（否则下游 rank_news 默认 neutral 导致排名偏低）
                direction = news.get("impact_direction", "neutral")
                if direction == "bullish":
                    news["impact_band"] = "bullish"
                elif direction == "bearish":
                    news["impact_band"] = "bearish"
                else:
                    news["impact_band"] = "neutral"
                # impact_direction / sentiment 保持信号情报原始值
                final_filtered.append(news)
                continue

            # NewsAnalysisItem 没有 impact_direction 字段，从 impact_band 推导方向
            direction = llm_res.get("impact_direction")
            if not direction:
                band = llm_res.get("impact_band", "neutral")
                # model_dump() 返回枚举对象，需要取 .value 得到字符串
                band_str = band.value if hasattr(band, "value") else str(band)
                if "bullish" in band_str:
                    direction = "bullish"
                elif "bearish" in band_str:
                    direction = "bearish"
                else:
                    direction = "neutral"
            # 1. 降级兜底: 仅当 LLM 异常未返回 band/方向时, 才用规则兜底
            if not llm_res.get("impact_band"):
                direction = predict_direction_by_rules(news.get("title", ""), news.get("content", ""))
                news["market_impact_score"] = 5.0 if direction != "neutral" else 3.0
                news["impact_direction"] = direction
                news["affected_sectors"] = infer_sectors_by_rules(
                    news.get("title", ""), news.get("content", ""), news.get("name", ""))
                news["affected_stocks"] = news.get("affected_stocks", [])
                news["impact_reason"] = "大模型调用降级：基于规则系统自动分析"
                news["sentiment"] = direction
                news["impact_band"] = "neutral"
                news["confidence"] = "low"
                news["influence_scope"] = "stock"
                news["analysis_chain"] = ""
            else:
                # 2. 正常 LLM 输出: 尊重 LLM 方向, 但对中性结论做精细纠偏
                #    仅当 LLM=neutral 且规则发现明确强正向/强负向组合时才纠偏
                #    (使用改进后的规则: 强正向组合优先, 不会利空优先误判)
                if direction == "neutral":
                    rule_dir = predict_direction_by_rules(news.get("title", ""), news.get("content", ""))
                    if rule_dir != "neutral":
                        direction = rule_dir
                        # 适度提升分数(+1), 不再无脑抬到5.0
                        orig_score = llm_res.get("market_impact_score", 3.0)
                        try:
                            orig_score = float(orig_score)
                        except Exception:
                            orig_score = 3.0
                        news["market_impact_score"] = min(orig_score + 1.0, 10.0)
                        llm_reason = llm_res.get("impact_reason", "")
                        news["impact_reason"] = (llm_reason + " | 规则纠偏: 检测到明确多空信号").strip(" |")
                    else:
                        news["market_impact_score"] = llm_res.get("market_impact_score", 3.0)
                        news["impact_reason"] = llm_res.get("impact_reason", "")
                else:
                    news["market_impact_score"] = llm_res.get("market_impact_score", 3.0)
                    news["impact_reason"] = llm_res.get("impact_reason", "")

                news["impact_direction"] = direction
                # 板块兜底: LLM 未填或空时用规则推断
                llm_sectors = llm_res.get("affected_sectors", [])
                if llm_sectors:
                    news["affected_sectors"] = llm_sectors
                else:
                    news["affected_sectors"] = infer_sectors_by_rules(
                        news.get("title", ""), news.get("content", ""), news.get("name", ""))
                news["affected_stocks"] = llm_res.get("affected_stocks", news.get("affected_stocks", []))
                news["sentiment"] = direction
                # model_dump() 返回枚举对象，转为字符串值存入 news（BAND_PRIORITY 等字典用字符串 key）
                band_val = llm_res.get("impact_band", "neutral")
                conf_val = llm_res.get("confidence", "medium")
                news["impact_band"] = band_val.value if hasattr(band_val, "value") else str(band_val)
                news["confidence"] = conf_val.value if hasattr(conf_val, "value") else str(conf_val)
                news["influence_scope"] = llm_res.get("influence_scope", "stock")
                news["analysis_chain"] = llm_res.get("analysis_chain", "")

            final_filtered.append(news)
        else:
            # LLM 在分析中判定为噪音并排除了该条目
            total_llm_removed += 1

    # 4. 生成返回状态
    msg = (
        f"[llm_filter] 完成：深度分析{len(prefiltered)}条，"
        f"保留{len(final_filtered)}条，过滤噪音{total_llm_removed}条。"
    )
    if batch_errors:
        msg += f" | {batch_errors}批降级 | 错误: {'; '.join(error_details[:2])}"

    return {
        "filtered_news": final_filtered,
        "messages": [AIMessage(content=msg)]
    }


# ============================================================
# 节点3: rank_news - 纯Python评分排名
# ============================================================

def rank_news_node(state: AgentState) -> dict:
    """纯Python评分排名: 可信度 x 重要度 + 时间衰减"""
    filtered_news = state.get("filtered_news", [])
    if not filtered_news:
        filtered_news = state.get("prefiltered_news", [])

    ranked = rank_news(filtered_news)

    top_score = ranked[0]["total_score"] if ranked else 0

    # 如果最终结果为空，提供明确的用户反馈
    msg_parts = [f"[rank_news] 排名完成: 共{len(ranked)}条, 最高分: {top_score:.4f}"]
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

    return {
        "ranked_news": ranked,
        "filtered_news": filtered_news,
        "messages": [
            AIMessage(content=" ".join(msg_parts))
        ]
    }

