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
from src.tools.calculators import rank_news, calculate_prefilter_importance, predict_direction_by_rules, infer_sectors_by_rules, score_news_relevance
from src.agent.state import AgentState
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
        "max_tokens": 4096
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

        if attempt < max_retries:
            # 指数退避: 2s, 4s
            wait_time = 2 ** (attempt + 1)
            logger.info(f"等待 {wait_time}s 后重试...")
            time.sleep(wait_time)
        session.close()

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

    return {
        "raw_news": all_news,
        "announcements": announcements,
        "messages": [
            AIMessage(content=(
                f"[fetch_news] 已获取当日全部资讯："
                f"新闻{len(raw_news)}条 + 公告{len(announcements)}条 + "
                f"信号情报{len(market_signals)}条 = 合计{len(all_news)}条"
            ))
        ]
    }


# ============================================================
# Stage 1: Python预过滤 (毫秒级)
# ============================================================

# 硬件科技核心关键词 (预过滤保底: 即使重要度不高也优先保留)
TECH_HARDWARE_KEYWORDS = [
    "CPO", "光模块", "光连接", "光通信", "硅光", "光电",
    "PCB", "覆铜板", "线路板", "HDI",
    "半导体", "芯片", "封测", "晶圆", "光刻", "EDA", "存储芯片",
    "算力", "服务器", "交换机", "液冷", "散热",
    "HBM", "DDR5", "先进封装", "CoWoS",
    "英伟达", "AMD", "台积电", "海力士",
]


def _calc_similarity(text1: str, text2: str) -> float:
    # 简单的2-gram字符级别Jaccard相似度
    set1 = set([text1[i:i+2] for i in range(len(text1)-1)])
    set2 = set([text2[i:i+2] for i in range(len(text2)-1)])
    if not set1 or not set2:
        return 0.0
    return len(set1 & set2) / len(set1 | set2)


_PREFILTER_QUOTA = {"direct": None, "sector": 20, "macro": 10}
_PREFILTER_TOTAL_LIMIT = 40


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

    total_removed = dup_count + (len(deduped) - len(kept))
    return kept, total_removed


# ============================================================
# Stage 2: LLM分析 (标签化)
# ============================================================

ANALYSIS_PROMPT = """你是资深A股资讯分析师。请对以下资讯进行分析。

## 资讯列表（共{n}条）
{news_list}

请完成以下分析:

### 1. 噪音识别
识别并过滤剩余噪音:
- 公司庆典、年会、获奖、表彰等公关活动
- 旧闻重复、过时信息
- 娱乐八卦、社会新闻
- 广告软文、营销推广
- 与A股市场无直接关联的内容

### 2. 影响分析与打分 (保留所有非噪音资讯)
对每条保留的资讯客观分析其市场影响：
- market_impact_score: 市场影响力打分(0-10分，请拉开分值差距，避免集中在中庸分数):
  * 9-10分: 国家级宏观政策（如降准、降息）、重大系统性黑天鹅、千亿级龙头生死攸关的事件
  * 7-8分: 板块级重大利好/利空、知名企业重大变动（如并购重组、立案调查等）
  * 4-6分: 普通个股常规经营利好/利空（如业绩预增、大股东减持）、重要行业日常数据更新
  * 1-3分: 边缘个股无关痛痒常规动态（如召开股东大会）、日常无增量信息的股评
- impact_direction: 必须根据以下原则进行客观的多空倾向判定，严禁将明显有利好或利空倾向的资讯保守地判定为中性(neutral)：
  * bullish (利好): 适用于公司业绩预增/翻倍、大额签约/中标、政策扶持、大股东增持/回购、兼并重组利好、技术突破、产品提价等。
  * bearish (利空): 适用于立案调查、违规处罚、业绩爆雷/亏损、大股东减持、债务违约、产品降价、行业利空政策等。
  * neutral (中性): 仅适用于无明显多空倾向的常规行业宏观播报、边缘数据更新、无实质性增量信息的日常公告、股评分析等。
  * 特别规则(科技板块): 当资讯涉及半导体/芯片/CPO/光模块/PCB/算力/服务器/HBM/封测等硬件科技核心板块时，impact_direction 必须以"对科技板块的影响"为判定依据。例如"存储芯片涨价"对科技板块属利好(bullish)，"芯片降价打价格战"对科技板块属利空(bearish)。
- affected_sectors: 影响的板块（如 ["半导体", "新能源"] ）。**必须填写**: 只要资讯涉及任何行业/板块, 就必须提取对应的板块名称, 严禁留空数组。常见板块包括: 半导体/芯片、CPO/光通信、PCB/电路板、算力/服务器、存储/HBM、新能源、光伏、储能、人工智能、医药、白酒、银行、房地产、军工、煤炭、钢铁、汽车、锂电池、机器人、消费电子等。
- affected_stocks: 明确提及的个股（如 ["贵州茅台", "宁德时代"] ）
- impact_reason: 一句话说明影响逻辑

重要: 利好和利空资讯都应保留, 客观打分并标注方向。方向判定要果断, 含明确多空信号的严禁判中性。

请以JSON格式返回（只返回JSON，不要其他文字）:
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
      "impact_direction": "bullish",
      "affected_sectors": ["半导体"],
      "affected_stocks": ["中芯国际"],
      "impact_reason": "半导体国产替代加速，利好板块龙头"
    }}
  ],
  "removed_count": 去除的噪音数量,
  "analysis_summary": "本次分析简要摘要（一句话）"
}}
```

注意:
- filtered_news 中的每条必须保留原始字段，并新增 market_impact_score 和 impact_* 字段
- removed_count 只包含噪音数量
- 如果资讯内容不足以判断影响方向，impact_direction 设为 "neutral"，打分酌情降低
- affected_sectors 必须尽力提取, 仅当资讯完全不涉及任何行业板块时才设为空数组 []
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
    cleaned = re.sub(r'[^\x20-\x7E\xA0-\xFF\u4e00-\u9fff]', ' ', cleaned)  # 保留可见字符和中文字符

    if "```json" in cleaned:
        cleaned = cleaned.split("```json")[1].split("```")[0]
    elif "```" in cleaned:
        cleaned = cleaned.split("```")[1].split("```")[0]

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



def _llm_analyze_batch(news_batch: list) -> list:
    """调用LLM分析一批资讯, 返回分析后的资讯列表"""
    import logging
    logger = logging.getLogger(__name__)

    # 截断content减少token, 加速LLM响应
    truncated_batch = []
    for n in news_batch:
        item = dict(n)
        content = item.get("content", "")
        if len(content) > 100:
            item["content"] = content[:100] + "..."
        truncated_batch.append(item)

    prompt = ANALYSIS_PROMPT.format(
        n=len(truncated_batch),
        news_list=json.dumps(truncated_batch, ensure_ascii=False, indent=2)
    )

    system_msg = "你是资深A股资讯分析师，善于从海量资讯中识别噪音、判断多空方向、分析板块影响。请只返回JSON，不要在JSON字符串中使用换行符。"

    try:
        content = _call_llm_api(system_msg, prompt, timeout=90, max_retries=2)
    except Exception as e:
        logger.error(f"LLM API 调用最终失败: {e}")
        # 全部降级返回原始数据
        return truncated_batch

    logger.info(f"LLM返回长度: {len(content)}, 前100字: {content[:100]}")

    filtered = result.get("filtered_news", [])

    # 我们已经改为在 llm_filter_node 中基于 title 原地合并回原始 news 字典
    # 这里只需要保证输出的格式对得齐 title 和新增字段即可
    for item in filtered:
        item.setdefault("market_impact_score", 3.0)
        item.setdefault("impact_direction", "neutral")
        item.setdefault("affected_sectors", [])
        item.setdefault("affected_stocks", [])
        item.setdefault("impact_reason", "")
        item.setdefault("sentiment", "neutral")

    return filtered


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
    prefiltered, py_removed = _python_prefilter(raw_news, top_n=30)

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
    - 如果 prefiltered 为空，直达排名；
    - 如果预筛列表中包含重磅信号（科技硬件/政策/ST等），必须走 LLM 深度分析；
    - 如果预筛列表很少（<=3条）且无重磅信号，可跳过 LLM 直接排名节省时间；
    - 否则默认走 LLM 分析。
    """
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
        from concurrent.futures import ThreadPoolExecutor, as_completed
        max_workers = min(len(batches), 4)
        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = {executor.submit(_llm_analyze_batch, batch): idx for idx, batch in enumerate(batches)}

        results_map = {}
        try:
            for future in as_completed(futures, timeout=120):
                idx = futures[future]
                try:
                    results_map[idx] = future.result(timeout=90)
                    logger.info(f"LLM 批次 {idx} 分析完成")
                except Exception as e:
                    results_map[idx] = None
                    batch_errors += 1
                    error_details.append(f"批次{idx}: {str(e)[:80]}")
                    logger.warning(f"LLM 批次 {idx} 失败: {e}")
        except Exception as e:
            logger.warning(f"LLM 并行超时: {e}")
        finally:
            executor.shutdown(wait=False)

        # 合并 LLM 结果
        for idx in range(len(batches)):
            batch = batches[idx]
            filtered_batch = results_map.get(idx)
            if filtered_batch:
                all_llm_results.extend(filtered_batch)
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
    llm_results_by_title = {n.get("title", "").strip(): n for n in all_llm_results}

    for news in prefiltered:
        title = news.get("title", "").strip()
        if title in llm_results_by_title:
            # LLM 没有认为是噪音，合并字段
            llm_res = llm_results_by_title[title]

            # 信号情报: 方向已由交易所官方数据锁定, 不被 LLM 覆盖
            if news.get("category") == "signal":
                news["market_impact_score"] = llm_res.get("market_impact_score", news.get("market_impact_score", 5.0))
                news["affected_sectors"] = llm_res.get("affected_sectors", news.get("affected_sectors", []))
                news["affected_stocks"] = llm_res.get("affected_stocks", news.get("affected_stocks", []))
                if llm_res.get("impact_reason"):
                    news["impact_reason"] = llm_res["impact_reason"]
                # impact_direction / sentiment 保持信号情报原始值
                final_filtered.append(news)
                continue

            direction = llm_res.get("impact_direction")
            # 1. 降级兜底: 仅当 LLM 异常未返回方向时, 才用规则兜底
            if not direction:
                direction = predict_direction_by_rules(news.get("title", ""), news.get("content", ""))
                news["market_impact_score"] = 5.0 if direction != "neutral" else 3.0
                news["impact_direction"] = direction
                news["affected_sectors"] = infer_sectors_by_rules(
                    news.get("title", ""), news.get("content", ""), news.get("name", ""))
                news["affected_stocks"] = news.get("affected_stocks", [])
                news["impact_reason"] = "大模型调用降级：基于规则系统自动分析"
                news["sentiment"] = direction
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
        msg_parts.append(
            f" | 注意: 全部数据被过滤！原始获取{raw_count}条, "
            f"预筛保留{pre_count}条, LLM过滤后剩余0条。"
        )
        if raw_count == 0:
            msg_parts.append(" 可能原因: 数据源全部不可用或网络异常。")

    return {
        "ranked_news": ranked,
        "filtered_news": filtered_news,
        "messages": [
            AIMessage(content=" ".join(msg_parts))
        ]
    }

