# filepath: src/agent/nodes.py
"""
A股资讯监测 Agent 节点实现
3个节点: fetch_news -> filter_noise -> rank_news

两阶段过滤方案 (解决当日资讯量可能数百上千条的问题):
  Stage 1 - Python预过滤 (毫秒级, 处理数百条->约20条):
    - 关键词去噪 (庆典/年会/八卦等)
    - 标题去重
    - 重要度初筛 (取top 20)
  Stage 2 - LLM分析 (每批10条约25秒):
    - 剩余噪音识别
    - 利好/利空方向标注
    - 影响板块/个股提取
"""
import json
import re
import time
import logging
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

from src.config import OPENROUTER_API_KEY, OPENROUTER_MODEL_NAME, OPENROUTER_BASE_URL, IS_OPENROUTER_OFFICIAL
from src.tools.data_fetchers import get_stock_news, get_announcements, get_market_signals, dedup_news_3layer
from src.tools.calculators import rank_news, predict_direction_by_rules, infer_sectors_by_rules, score_news_relevance, TECH_HARDWARE_KEYWORDS, SECTOR_KEYWORDS, _load_watchlist, is_leader_or_high_impact, _is_self_only_individual_stock, dedup_and_cap_for_display, _has_tech_keyword, _TECH_ENGLISH_WORDS
from src.tools.data_fetchers import get_hs300_constituents
# 高信号关键词表已收敛至共享模块（与实时推送脚本共用单一事实来源，避免漂移）
from src.tools.keyword_tables import HIGH_SIGNAL_KEYWORDS
from src.agent.state import AgentState, NO_DATA_SENTINEL
from src.schemas import ImpactBand, NewsAnalysisItem


def _call_llm_api(system_prompt: str, user_prompt: str, timeout: int = 90, max_retries: int = 2, deadline: float = 0) -> str:
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
    # OpenRouter 官方要求 HTTP-Referer 和 X-Title 请求头，否则返回 402
    if IS_OPENROUTER_OFFICIAL:
        headers["HTTP-Referer"] = "https://github.com/stock-news-agent"
        headers["X-Title"] = "StockNewsAgent"
    payload = {
        "model": OPENROUTER_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,  # 与方式A (LangChain) 保持一致，结构化输出场景降低温度
        "max_tokens": 16384
    }

    last_error = None
    for attempt in range(max_retries + 1):
        # 总超时熔断：逼近 deadline 立即放弃重试并返回上层降级，
        # 根治方式B/C 内部重试叠加（最多 4×120s=480s）突破 llm_filter 的 300s 总超时导致"一直超时"
        if deadline and time.monotonic() >= deadline:
            raise Exception(f"LLM 调用逼近总超时熔断，放弃重试（已尝试 {attempt} 次）")
        session = requests.Session()
        # 官方端点(OpenRouter)需科学上网保留代理；Agnes 等中转端点禁用代理避免 ConnectionRefused
        session.trust_env = IS_OPENROUTER_OFFICIAL
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
            # 重试前再次确认未超 deadline（避免退避等待期间已超时仍继续重试）
            if deadline and time.monotonic() >= deadline:
                raise Exception(f"LLM 调用逼近总超时熔断，放弃重试（已尝试 {attempt + 1} 次）")
            # 指数退避: 2s, 4s
            wait_time = 2 ** (attempt + 1)
            logger.info(f"等待 {wait_time}s 后重试（Agnes 端点）...")
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
    all_news = dedup_news_3layer(all_news)
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

_PREFILTER_TOTAL_LIMIT = 20
# 各类最小保底配额：确保冷门类别不被自适应比例饿死
# macro 保底4：核心外围传导资讯（美联储/制裁/地缘）需保证配额
_PREFILTER_MIN_QUOTA = {"sector": 6, "macro": 4}


def _adaptive_quota(buckets: dict, total_limit: int = _PREFILTER_TOTAL_LIMIT) -> dict:
    """按实际命中数量比例分配 sector/macro 配额（direct 过多时也参与截断）

    自适应逻辑：
      1. direct 数量未超 total_limit - min_quota_sum 时全留；
         超过时 direct 截断到 total_limit - min_quota_sum，保证 sector/macro 保底
      2. 剩余配额 = total_limit - direct_quota（不低于 min_quota_sum）
      3. sector/macro 按实际命中数比例瓜分剩余配额
      4. sector 保底提升后重新计算 macro，防止保底提升导致总和超过 remaining
      5. 回补：sector/macro 实际数量不足配额时，剩余槽位还给 direct（避免浪费）

    Returns:
        {"direct": int|None, "sector": int, "macro": int}  # None 表示不截断
    """
    direct_count = len(buckets["direct"])
    min_total = _PREFILTER_MIN_QUOTA["sector"] + _PREFILTER_MIN_QUOTA["macro"]

    # direct 数量过多时也参与截断，保证 sector/macro 保底不被全局截断吃掉
    if direct_count > total_limit - min_total:
        direct_quota = total_limit - min_total
        remaining = min_total
    else:
        direct_quota = None  # 全留
        remaining = max(total_limit - direct_count, min_total)

    sector_avail = len(buckets["sector"])
    macro_avail = len(buckets["macro"])
    total_avail = sector_avail + macro_avail

    if total_avail == 0:
        sector_quota = 0
        macro_quota = 0
    elif total_avail <= remaining:
        # sector/macro 总量不超过剩余配额 → 全留
        sector_quota = sector_avail
        macro_quota = macro_avail
    else:
        # 按比例分配
        sector_quota = round(remaining * sector_avail / total_avail)
        # 保底提升后重新计算 macro，防止保底提升导致总和超过 remaining
        sector_quota = max(min(sector_quota, sector_avail), min(_PREFILTER_MIN_QUOTA["sector"], sector_avail))
        macro_quota = remaining - sector_quota
        macro_quota = max(min(macro_quota, macro_avail), min(_PREFILTER_MIN_QUOTA["macro"], macro_avail))

    # 回补：sector/macro 用不完的槽位还给 direct，避免预筛总数 < top_n
    if direct_quota is not None:
        used = direct_quota + sector_quota + macro_quota
        if used < total_limit:
            direct_quota = direct_quota + (total_limit - used)

    return {"direct": direct_quota, "sector": sector_quota, "macro": macro_quota}


def _python_prefilter(news_list: list, top_n: int = _PREFILTER_TOTAL_LIMIT) -> tuple:
    """Python 预筛：权重表打分 + 自适应配额截断 + 聚类热度

    流程：
      1. 三层去重（URL/标题/SimHash）
      2. 对每条调 score_news_relevance 打分分类
      3. 按自适应配额截断：direct全留, sector/macro按命中比例瓜分剩余配额
      4. 计算聚类热度 cluster_weight

    注：噪音过滤交由权重表负责——零关联度且 sector 类的条目视为纯噪音丢弃，
        不再使用 nodes.py 内的 NOISE_KEYWORDS 黑名单（已删除）。
    """
    deduped = dedup_news_3layer(news_list)
    dup_count = len(news_list) - len(deduped)

    # 公告预过滤：非龙头股的低影响常规公告直接丢弃
    # 避免大量 *ST 小票的董事会决议/章程修订/独董声明等占用预筛配额
    # get_hs300_constituents 失败时返回空集合，is_leader_or_high_impact 保守不过滤
    try:
        hs300 = get_hs300_constituents()
    except Exception as e:
        logger.warning(f"[prefilter] 获取沪深300成分股失败，跳过公告预过滤: {e}")
        hs300 = {"codes": set(), "names": set()}
    before_ann_filter = len(deduped)
    filtered = []
    ann_removed = 0
    for news in deduped:
        if news.get("category") == "announcement":
            if not is_leader_or_high_impact(news, hs300):
                ann_removed += 1
                continue
        filtered.append(news)
    deduped = filtered
    if ann_removed > 0:
        logger.info(f"[prefilter] 非龙头低影响公告过滤: 移除{ann_removed}条, 剩余{len(deduped)}条")

    # 加载关注列表：命中关注个股的新闻提升为 direct 类
    watchlist = _load_watchlist()
    watch_stocks = list(watchlist.get("stocks", []))

    for news in deduped:
        score, category = score_news_relevance(news)
        # 检查是否命中 watchlist 个股，命中则用股票名重新打分
        if watch_stocks:
            title = news.get("title", "")
            content = news.get("content", "")
            text = f"{title} {content}"
            for ws in watch_stocks:
                if ws in text:
                    s, c = score_news_relevance(news, stock_name=ws)
                    if s > score:
                        score, category = s, c
                    break
        news["_prefilter_score"] = score
        news["_prefilter_category"] = category

    # 权重表初筛：丢弃零关联度的纯噪音条目（所有类别一视同仁，macro 零分也丢弃）
    scored = [n for n in deduped if n["_prefilter_score"] > 0]

    # 科技/A股相关性过滤：本系统是科技板块资讯监测，
    # 既无科技关键词、又无A股板块词、又无核心外围传导词的纯外围/纯非科技资讯直接剔除
    # （避免阿迪达斯收入预期、澳大利亚央行、纯白酒/银行板块等与A股科技无关的资讯占用配额）
    _CORE_MACRO_TRANSMISSION = {
        # 科技管制（直接传导A股科技）
        "制裁", "出口管制", "禁运", "实体清单", "关税", "贸易战",
        # 全球系统性风险
        "美联储", "加息", "降息", "缩表", "QE", "鲍威尔", "非农", "CPI", "PPI",
        "熔断", "崩盘", "债务危机", "银行危机", "金融风险", "系统性风险",
        # 地缘冲突
        "战争", "军事冲突", "冲突", "地缘", "俄乌", "中东", "台海",
        # A股直接政策
        "降准", "印花税", "注册制", "北向资金",
        # A股大盘
        "A股", "沪指", "深证", "创业板", "沪深300", "大盘",
        # 外围科技指数（直接传导A股科技情绪）
        "纳指", "纳斯达克", "美股", "恒生科技", "费城半导体",
    }
    # 科技板块专属词：SECTOR_KEYWORDS 去除纯非科技词（白酒/银行/房地产/煤炭/钢铁/有色/医药），
    # 补充 TECH_HARDWARE_KEYWORDS 未覆盖的科技延伸词
    _TECH_SECTOR_TERMS = {
        "新能源", "光伏", "储能", "人工智能", "AI", "大模型",
        "算力", "数据要素", "消费电子", "汽车电子", "锂电", "氢能", "机器人",
        "电子", "通信", "软件", "计算机", "信息技术", "数字经济",
        "智能制造", "自动化", "量子", "元宇宙",
    }
    macro_filtered = 0
    relevant = []
    for news in scored:
        text = f"{news.get('title', '')} {news.get('content', '')}"
        # 词边界感知的科技词匹配（英文缩写不子串匹配，避免 nAMD 等误命中）
        has_tech = _has_tech_keyword(text)
        has_tech_sector = any(kw in text for kw in _TECH_SECTOR_TERMS)
        has_macro_transmission = any(kw in text for kw in _CORE_MACRO_TRANSMISSION)
        if not has_tech and not has_tech_sector and not has_macro_transmission:
            macro_filtered += 1
            continue
        relevant.append(news)
    if macro_filtered > 0:
        logger.info(f"[prefilter] 科技/A股相关性过滤: 剔除{macro_filtered}条无关外围资讯")
    scored = relevant

    buckets = {"direct": [], "sector": [], "macro": []}
    self_only_removed = 0
    for news in scored:
        cat = news["_prefilter_category"]
        # 预筛剔除：提及具体非龙头个股、且无板块/宏观联动的资讯（仅影响个股自身），
        # 不具备市场/板块带动价值，在预筛阶段直接剔除（用户需求：最终只保留能带动市场情绪的价值资讯）。
        # 公告类已在上方 is_leader_or_high_impact 中处理；此处 _is_self_only_individual_stock 对公告返回 False。
        if _is_self_only_individual_stock(news, hs300):
            self_only_removed += 1
            continue
        buckets[cat].append(news)
    if self_only_removed > 0:
        logger.info(f"[prefilter] 剔除仅影响个股自身的非龙头资讯: {self_only_removed}条")

    for cat in buckets:
        buckets[cat].sort(key=lambda x: x["_prefilter_score"], reverse=True)

    # 自适应配额：按实际命中比例分配，替代原先硬编码的 sector30/macro15
    quota = _adaptive_quota(buckets, total_limit=top_n)

    kept = []
    for cat, q in quota.items():
        if q is None:
            kept.extend(buckets[cat])
        else:
            kept.extend(buckets[cat][:q])

    # 安全兜底：配额分配已保证不超 top_n，此处仅防御性截断
    if len(kept) > top_n:
        logger.warning(f"预筛配额分配后仍超限: {len(kept)} > {top_n}, 按score截断")
        kept.sort(key=lambda x: x["_prefilter_score"], reverse=True)
        kept = kept[:top_n]

    # 聚类热度：标题字符集合 Jaccard，阈值 0.35。
    # 修复前用标题 2-gram Jaccard>0.35：同事件不同角度报道（如"铠侠NAND涨价" vs
    # "铠侠NAND需求强劲"）字面重合度低，实际数据中 cluster_weight 恒为 0，
    # NEWS_CLUSTER_WEIGHT=0.15 聚类热度因子名存实亡。
    # 只比较标题（不含 content）：公告/新闻正文模板化语言（"控股子公司/亿元/签订"）
    # 会把不同公司的无关事件误聚成高热度簇（实测 cw 虚高到 10）；标题是事件语义核心。
    # 0.35 低于展示层标题去重阈值 0.6，捕获同事件不同表述，模板化误聚仅产生低热度。
    _CLUSTER_JACCARD_THRESHOLD = 0.35
    cluster_sets = [set(news.get("title", "")) for news in kept]

    for i, news1 in enumerate(kept):
        cluster_size = 1
        cs1 = cluster_sets[i]
        for j, cs2 in enumerate(cluster_sets):
            if i != j and cs1 and cs2:
                union = len(cs1 | cs2)
                if union > 0 and len(cs1 & cs2) / union > _CLUSTER_JACCARD_THRESHOLD:
                    cluster_size += 1
        news1["cluster_weight"] = min(cluster_size - 1, 10)

    for news in kept:
        news.pop("_prefilter_score", None)
        news.pop("_prefilter_category", None)
        # 清理所有以 _ 开头的临时字段，防止泄漏到 LLM 输入
        for key in list(news.keys()):
            if key.startswith("_"):
                news.pop(key, None)

    # total_removed 含三层去重 + 非龙头低影响公告过滤 + 科技相关性过滤 + 自影响个股剔除 + 配额初筛
    # 注意: len(deduped) - len(kept) 已包含 self_only_removed（self-only 剔除发生在 scored→buckets 阶段，
    # scored 来源于 deduped），不可再加 self_only_removed 否则双重计数
    total_removed = dup_count + ann_removed + (len(deduped) - len(kept))
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

### 第2步：影响范围判断（影响排序加权，market 级在同强度下优先）
按以下顺序逐层判断，取最高层级作为 influence_scope。该字段参与最终排序加权：
market 级影响面最广，在影响强度相近时优先于 sector/stock 级。

1. 市场级(market)：能影响全球或全A股市场的宏观事件（影响面最广，同强度下优先）
   - 美联储政策（加息/降息/缩表/点阵图）、鲍威尔/非农/CPI 等全球货币政策信号
   - 重大地缘政治事件影响全球风险偏好（战争/冲突/制裁/能源危机）
   - 央行/财政部/国务院的全面性政策（降准降息、印花税调整、注册制改革）
   - 全球系统性金融风险（债务危机/银行危机/熔断）
   * 判定要点：该事件是否"牵动全球或全市场资金与情绪"，而非仅某一板块

2. 板块级(sector)：影响整个行业/板块（次级优先）
   - 行业政策（半导体补贴、新能源规划、AI 产业扶持）→ 整个板块
   - 龙头股重大事件 → 带动板块情绪和估值
     * 龙头判断标准：市值前列、板块风向标、机构持仓集中
     * 典型龙头：中际旭创/新易盛(CPO) | 宁德时代(新能源) | 贵州茅台(白酒) | 招商银行(银行) | 中芯国际(半导体) | 工业富联(算力)
   - 供应链传导（如上游涨价→下游成本上升→整个链条）
   - 板块性技术趋势（如CPO技术路线确立）

3. 个股级(stock)：仅影响个股本身（排最后）
   - 非龙头股的普通公告（业绩波动、常规经营）
   - 无板块联动效应的个股事件
   - 注意：即使是利好，如果只是个股层面且非龙头，不应判为 sector

### 第3步：方向判断（以科技板块为本位）
- 本系统是科技板块资讯监测，方向判定一律以"对科技板块的影响"为准
- 科技板块涨/利好 → bullish；科技板块跌/利空 → bearish
- 收盘播报/盘面汇总等混合资讯：即使白酒/消费/银行等非科技板块上涨，只要科技板块（半导体/电子/通信/算力/PCB/CPO/机器人等）下跌，即判 bearish
- 非科技板块（白酒/消费/医药/银行/汽车等）的利好不改变整体方向判定
- 含明确多空信号的严禁判 neutral/mixed
- 仅当科技板块自身多空交织（如半导体涨但通信跌）才判 mixed

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

## 输出契约（每条只输出以下字段，不要重复content/source等原始字段）
0. idx: 输入中的数字编号（必须原样回显，用于匹配，不要修改也不要省略）
1. title: 原标题（用于匹配，必须与输入一致）
2. market_impact_score: 0-10（0无影响/10极重大）
3. impact_band: 6档多空方向（与 score 独立，禁止按 score 区间反推 band）
   - bullish(强利好): 业绩预增/大额中标/政策扶持/增持回购/技术突破
   - mildly_bullish(弱利好): 普通经营利好
   - neutral(中性): 无明显多空的常规播报
   - mixed(多空交织): 同时含利好利空
   - mildly_bearish(弱利空): 普通经营利空
   - bearish(强利空): 立案/退市/爆雷/违约/重大处罚/板块暴跌/制裁升级
   * 关键：score 衡量"影响强度/重要性"(0-10)，band 衡量"多空方向"，两者相互独立。
     重大利空事件（如半导体板块跌超10%、美对华芯片制裁升级、龙头爆雷）影响极大，
     必须给高分(7-9)且 band=bearish，绝不能因"利空"就打低分——重要资讯无论利空利好都应排前。
4. confidence: high/medium/low
5. affected_sectors: 必填，涉及板块（半导体/CPO/PCB/算力/新能源/医药/银行/...）
6. affected_stocks: 明确提及的个股
7. impact_reason: 一句话影响逻辑
8. influence_scope: market/sector/stock（按第2步判断）
9. analysis_chain: 5步推理链（箭头连接，简明记录思考过程）

## 规则
- band（多空方向）与 score（影响强度）相互独立，禁止用 score 区间反推 band
- 含明确多空信号的严禁判 neutral/mixed
- 方向判定以科技板块为本位：科技板块跌即 bearish，非科技板块（白酒/消费等）上涨不改变方向
- **impact_reason 必须基于资讯实际内容**：先识别事件主体（哪个国家/公司/板块），再分析影响。
  严禁看到关键词就套用常见模板（如看到"301条款/关税"就写"中美贸易摩擦"，看到"制裁"就写"半导体制裁"）。
  若事件主体不是中国/中美，不得在 impact_reason 中出现"中美"字样。
- **affected_sectors 必须与资讯内容直接相关**：仅提取资讯正文中明确提及或直接涉及的板块，
  不得因关键词联想而添加未提及的板块（如资讯讲巴西关税就不应添加"半导体"除非正文明确涉及）。
- **influence_scope 必须严格按第2步判定**：仅影响个股自身且无板块联动的非龙头股资讯，influence_scope 必须判为 stock（这类资讯不具备市场/板块带动价值，将被沉底或预筛剔除）；不要因某条资讯带"利好"就擅自升为 sector/market
- 市场级(market)只给真正牵动全球或全市场资金情绪的宏观事件，普通外围个股波动、单一海外公司消息不得判为 market
- **每条必须回显 idx 字段**，值与输入对应条目的 idx 编号完全一致；切勿省略、切勿自造编号
- 噪音不输出到 filtered_news，计入 removed_count。噪音定义：
  - 庆典/八卦/软文/公关稿
  - 与A股无直接关联的纯国际资讯（如海外天气、韩国外汇案、非涉华国际事件）
    * 例外：影响全球市场或A股科技板块的外围资讯必须保留（如美联储政策、美对华科技制裁、地缘冲突升级、全球金融风险）
  - 纯宏观经济评论（无具体板块/个股影响逻辑）
  - 重复报道同一事件（只保留信息量最大的一条）
  - 非龙头股的常规公告（董事会决议/章程修订/独董声明/高管变更/会议通知等）
    * 例外：龙头股（沪深300成分股）的公告保留，非龙头股的高影响公告（立案/退市/重组/业绩预告/债务违约）也保留

请以JSON格式返回（只返回JSON，不要输出content/source/published_at等原始字段）:
```json
{{
  "filtered_news": [
    {{
      "idx": 0,
      "title": "原标题",
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
- filtered_news 中每条只输出idx/title和分析字段，不要输出content/source/published_at/category
- affected_sectors 必须尽力提取，仅当完全不涉及行业板块时才设为空数组
- analysis_chain 必须填写，记录5步推理过程
- analysis_chain尽量简短（一行内完成），impact_reason不超过30字
"""


def _repair_json(text: str) -> str:
    """尝试修复LLM返回的JSON格式问题"""
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
    # 转义JSON字符串值内的换行符（Claude等模型常返回未转义的换行）
    # 逐字符扫描：在双引号字符串内，把裸 \n \r \t 转义
    result = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            result.append(ch)
            escape = False
            continue
        if ch == '\\':
            result.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string:
            if ch == '\n':
                result.append('\\n')
            elif ch == '\r':
                result.append('\\r')
            elif ch == '\t':
                result.append('\\t')
            else:
                result.append(ch)
        else:
            result.append(ch)
    return ''.join(result)


def _safe_parse_json(content: str) -> dict:
    """安全解析LLM返回的JSON, 多重容错"""
    if not content or not content.strip():
        return {"filtered_news": [], "removed_count": 0}

    # 预处理: 清理可能的乱码
    import re
    cleaned = content
    cleaned = cleaned.replace('\u0000', '')  # 移除null字符
    # 先提取代码块（避免代码块标记被破坏），处理 ```json / ``` 等变体
    cb_match = re.search(r'```(?:json)?\s*\n?(.*?)```', cleaned, re.DOTALL)
    if cb_match:
        cleaned = cb_match.group(1)
    elif "```" in cleaned:
        parts = cleaned.split("```")
        if len(parts) >= 3:
            cleaned = parts[1]
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

    # 尝试提取 filtered_news 数组（re 已在模块顶部导入）
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

    # 截断JSON修复：LLM输出被max_tokens截断时，JSON不完整
    # 策略：逐字符扫描filtered_news数组，提取所有完整的{}对象
    # 长度上限保护：max_tokens=16384 限制了输出长度，超过 200KB 的文本几乎不可能出现，
    # 加上限防御恶意/异常输入导致逐字符扫描 O(n) 性能问题
    recovered_items = []
    fn_match = re.search(r'"filtered_news"\s*:\s*\[', cleaned)
    if fn_match and len(cleaned) <= 200_000:
        array_start = fn_match.end()  # 指向 [ 后第一个字符
        i = array_start
        obj_depth = 0
        obj_start = -1
        in_str = False
        escape = False
        while i < len(cleaned):
            ch = cleaned[i]
            if escape:
                escape = False
                i += 1
                continue
            if ch == '\\':
                escape = True
                i += 1
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '{':
                    if obj_depth == 0:
                        obj_start = i
                    obj_depth += 1
                elif ch == '}':
                    obj_depth -= 1
                    if obj_depth == 0 and obj_start >= 0:
                        obj_text = cleaned[obj_start:i+1]
                        try:
                            recovered_items.append(json.loads(obj_text))
                        except json.JSONDecodeError:
                            try:
                                recovered_items.append(json.loads(_repair_json(obj_text)))
                            except json.JSONDecodeError:
                                pass
                        obj_start = -1
            i += 1
    if recovered_items:
        logger.info(f"截断JSON修复: 恢复了 {len(recovered_items)} 条完整记录")
        return {"filtered_news": recovered_items, "removed_count": 0}

    # ranking 结构兜底提取（rerank 输出：{"ranking": [...]}）
    # 与 filtered_news 同理：非完美 JSON（尾部文本/未转义字符）导致首层 json.loads 失败时，
    # 逐字符扫描 ranking 数组，恢复所有完整 {} 对象（实跑实证：rerank 返回因非完美 JSON 被静默丢弃，
    # 导致"LLM 智能重排"从未生效）。
    rk_match = re.search(r'"ranking"\s*:\s*\[', cleaned)
    if rk_match and len(cleaned) <= 200_000:
        array_start = rk_match.end()
        recovered_ranking = []
        i = array_start
        obj_depth = 0
        obj_start = -1
        in_str = False
        escape = False
        while i < len(cleaned):
            ch = cleaned[i]
            if escape:
                escape = False
                i += 1
                continue
            if ch == '\\':
                escape = True
                i += 1
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '{':
                    if obj_depth == 0:
                        obj_start = i
                    obj_depth += 1
                elif ch == '}':
                    obj_depth -= 1
                    if obj_depth == 0 and obj_start >= 0:
                        obj_text = cleaned[obj_start:i+1]
                        try:
                            recovered_ranking.append(json.loads(obj_text))
                        except json.JSONDecodeError:
                            try:
                                recovered_ranking.append(json.loads(_repair_json(obj_text)))
                            except json.JSONDecodeError:
                                pass
                        obj_start = -1
            i += 1
        if recovered_ranking:
            logger.info(f"截断JSON修复(ranking): 恢复了 {len(recovered_ranking)} 条重排记录")
            return {"ranking": recovered_ranking, "filtered_news": []}

    # adjustments 结构兜底提取（LLM调分输出：{"adjustments": [...]}）
    # 与 filtered_news/ranking 同理：非完美 JSON 时逐字符扫描恢复完整 {} 对象
    adj_match = re.search(r'"adjustments"\s*:\s*\[', cleaned)
    if adj_match and len(cleaned) <= 200_000:
        array_start = adj_match.end()
        recovered_adjustments = []
        i = array_start
        obj_depth = 0
        obj_start = -1
        in_str = False
        escape = False
        while i < len(cleaned):
            ch = cleaned[i]
            if escape:
                escape = False
                i += 1
                continue
            if ch == '\\':
                escape = True
                i += 1
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '{':
                    if obj_depth == 0:
                        obj_start = i
                    obj_depth += 1
                elif ch == '}':
                    obj_depth -= 1
                    if obj_depth == 0 and obj_start >= 0:
                        obj_text = cleaned[obj_start:i+1]
                        try:
                            recovered_adjustments.append(json.loads(obj_text))
                        except json.JSONDecodeError:
                            try:
                                recovered_adjustments.append(json.loads(_repair_json(obj_text)))
                            except json.JSONDecodeError:
                                pass
                        obj_start = -1
            i += 1
        if recovered_adjustments:
            logger.info(f"截断JSON修复(adjustments): 恢复了 {len(recovered_adjustments)} 条调分记录")
            return {"adjustments": recovered_adjustments, "filtered_news": []}

    # 最终降级: 返回空结果而不是抛出异常
    logger.warning(f"JSON解析最终失败，降级返回空结果: {cleaned[:100]}...")
    return {"filtered_news": [], "removed_count": 0}


# ============================================================
# 冲突护栏（借鉴 DSA score_action_conflicts_without_guardrail）
# ============================================================
# 注：原 BAND_SCORE_RANGE + _band_from_score 已删除——它们按 score 反推 band，
# 与"score(影响强度) 和 band(多空方向) 相互独立"的解耦设计直接矛盾，且全链路无调用。


def _band_to_direction(band: ImpactBand) -> str:
    """band → 3 档 direction（排名公式兼容）"""
    bullish = {ImpactBand.BULLISH, ImpactBand.MILDLY_BULLISH}
    bearish = {ImpactBand.BEARISH, ImpactBand.MILDLY_BEARISH}
    if band in bullish:
        return "bullish"
    if band in bearish:
        return "bearish"
    return "neutral"


# 利空/利好文本信号词（用于检测 LLM band 与分析文本自相矛盾）
# 扩充: 熔断/跌至/逊于预期/走低 等词缺失曾导致文本方向误判（实证: 韩股熔断/爱马仕跌至低点）
_BEARISH_TEXT_SIGNALS = {"利空", "暴跌", "下跌", "亏损", "减持", "处罚", "立案", "退市",
                         "爆雷", "违约", "承压", "下滑", "萎缩", "受挫", "受阻", "负面",
                         "恶化", "巨亏", "重挫", "闪崩", "崩盘", "跌停", "大亏",
                         "熔断", "跌至", "走低", "下挫", "恐慌", "抛售", "逊于预期",
                         "低于预期", "不及预期", "创新低", "回调", "搁浅", "叫停",
                         "风险警示", "制裁", "管制", "禁运", "关税"}
_BULLISH_TEXT_SIGNALS = {"利好", "暴涨", "上涨", "盈利", "增持", "回购", "补贴", "扶持",
                        "突破", "超预期", "预增", "增长", "提振", "刺激", "宽松", "涨停",
                        "大涨", "走强", "回暖",
                        "走高", "净流入", "流入", "创新高", "涨超", "扭亏", "中标",
                        "签约", "扩产", "满产", "订单饱满"}

# 低分方向性 band 强制中性阈值：弱信号（sc<该值）不带动市场情绪，
# LLM 误标的方向性 band（bullish/bearish）一律中性化，避免低分矛盾标注。
LOW_SCORE_NEUTRAL_THRESHOLD = 4.0

# 地缘风险/冲突负面信号：事件本身代表 global risk-off，不应被 LLM 误标为 bullish。
# 实证：#9「美军警告驻中东士兵」sc=8.0 标 bullish，实为地缘风险（risk-off）。
# 注意：仅用复合短语，避免裸"冲突/军事/战事"误伤普通文本（如"测试冲突业绩预增"）。
_GEOPOLITICAL_RISK_SIGNALS = {
    "战争", "导弹", "空袭", "军事行动", "军事打击", "军事警告", "制裁升级",
    "地缘紧张", "地缘风险", "局势紧张", "局势动荡", "战争阴云", "交火", "武装冲突",
    "边境冲突", "risk-off", "避险情绪", "军事演习", "军事部署", "战云", "紧张局势",
    "地缘局势", "冲突升级",
}
# 受益涨幅信号：若地缘事件同时描述某资产/板块明确受益（原油/黄金/军工订单等），
# 则 LLM 标 bullish 有依据，不应翻转。用于降低地缘校正的误翻率。
_BENEFIT_SIGNALS = {
    "涨", "利好", "订单", "中标", "突破", "创新高", "大涨", "上涨", "净流入",
    "扩产", "签约", "受益", "提振", "飙升", "走高", "拉升",
}


def _has_geopolitical_risk(text: str) -> bool:
    """检测文本是否描述地缘风险/冲突（global risk-off 事件）

    必须命中风险词且无明确受益涨幅词，避免误翻真正的军工/原油/黄金利好
    （如「原油大涨因地缘冲突」含「涨」，LLM 标 bullish 合理，不翻转）。
    """
    t = str(text)
    has_risk = any(s in t for s in _GEOPOLITICAL_RISK_SIGNALS)
    has_benefit = any(s in t for s in _BENEFIT_SIGNALS)
    return has_risk and not has_benefit


def _infer_direction_from_text(reason: str, chain: str = "") -> str:
    """从 impact_reason（自由文本结论）推断多空方向

    用于检测 LLM band 标签与分析文本自相矛盾的情况
    （如 band=bullish 但 reason 写"业绩暴雷利空"）

    关键约束：方向推断**仅基于 impact_reason**，不并入 analysis_chain。
    analysis_chain 是 LLM 的结构化推理标签（如"弱利空""强利好"），会回显 band
    方向，并入后会污染判断——实证：寒武纪章程修订 reason="常规章程修订,无实质经营影响"
    本应判 neutral，但其 chain 含"弱利空"(含"利空")，若并入则误判 bearish 导致
    中性护栏被跳过、常规公告被错标利空。chain 仅在 reason 为空时兜底。

    Returns:
        "bullish" / "bearish" / "neutral"（neutral=无法确定，不纠偏）
    """
    text = str(reason)
    if not text.strip():
        text = str(chain)  # reason 为空才退用 chain
    bearish_hits = sum(1 for kw in _BEARISH_TEXT_SIGNALS if kw in text)
    bullish_hits = sum(1 for kw in _BULLISH_TEXT_SIGNALS if kw in text)
    if bearish_hits > 0 and bullish_hits == 0:
        return "bearish"
    if bullish_hits > 0 and bearish_hits == 0:
        return "bullish"
    return "neutral"


# 显式中性标记短语：LLM 在 reason/chain 中明确写明"无明确多空信号/无实质影响"等，
# 即使 band 被标为方向性（如 mildly_bullish，因"回购"关键词触发 bullish 文本检测），
# 也应强制判为中性（阿特斯"回购进展公告"实证：reason 写"无明确多空信号"却被标 mildly_bullish）。
_NEUTRAL_MARKER_PHRASES = [
    "无明确多空信号", "无实质影响", "无重大影响", "无明显影响", "无实际影响",
    "无明确方向", "无重大变动", "无重大变化", "影响中性", "常规披露", "例行披露",
    "常规事项", "无具体影响", "中性看待",
]


def _has_explicit_neutral_marker(text: str) -> bool:
    """检测 LLM 是否明确声明该资讯无明确多空方向（应强制中性，盖过关键词误触发）"""
    t = str(text)
    return any(p in t for p in _NEUTRAL_MARKER_PHRASES)


def _apply_guardrails(items: list) -> list:
    """band 与 score 冲突时按 score 强制校正 band，并同步 direction/sentiment

    额外校验：LLM 可能 band 标 bullish 但 reason/chain 文本写利空（score 误打高分），
    此时以分析文本方向为准校正 band 方向，但保持 LLM 判断的强度级别（镜像翻转）。
    强弱档位由 LLM 的 impact_band 决定，不用 score 阈值重新计算：
      bullish(强利好)↔bearish(强利空), mildly_bullish(弱利好)↔mildly_bearish(弱利空)
    """
    # 方向镜像翻转表：保持 LLM 判断的强度级别，只翻转多空方向
    _FLIP_TO_BEARISH = {"bullish": ImpactBand.BEARISH, "mildly_bullish": ImpactBand.MILDLY_BEARISH,
                        "mixed": ImpactBand.MILDLY_BEARISH}
    _FLIP_TO_BULLISH = {"bearish": ImpactBand.BULLISH, "mildly_bearish": ImpactBand.MILDLY_BULLISH,
                        "mixed": ImpactBand.MILDLY_BULLISH}

    for item in items:
        # 兼容 dict 和 NewsAnalysisItem (Pydantic BaseModel) 两种输入：
        # 生产管线传 dict（_normalize_llm_item 返回），单元测试传 NewsAnalysisItem
        is_model = isinstance(item, NewsAnalysisItem)
        if is_model:
            d = item.model_dump()
        elif isinstance(item, dict):
            d = item
        else:
            continue
        band_str = d.get("impact_band", "neutral")
        try:
            band = ImpactBand(band_str)
        except ValueError:
            band = ImpactBand.NEUTRAL
        # 显式中性标记强制中性（阿特斯"回购进展公告"实证：LLM 标 mildly_bullish 但 reason 写"无明确多空信号"）
        _marker_text = str(d.get("impact_reason", "")) + " " + str(d.get("analysis_chain", ""))
        if _has_explicit_neutral_marker(_marker_text):
            band = ImpactBand.NEUTRAL
            d["impact_band"] = "neutral"
            d["impact_direction"] = "neutral"
            d["sentiment"] = "neutral"
            if is_model:
                item.impact_band = ImpactBand.NEUTRAL
                item.sentiment = "neutral"
            continue  # 跳过下方 score-band 校正，避免 5.5 分被翻回 mildly_bullish
        try:
            score = float(d.get("market_impact_score", 3.0))
        except (ValueError, TypeError):
            score = 3.0
        # 文本方向校验：band 与 reason/chain 文本矛盾时，镜像翻转方向保持强度级别
        text_dir = _infer_direction_from_text(
            str(d.get("impact_reason", "")), str(d.get("analysis_chain", "")))
        if text_dir == "bearish" and band.value in _FLIP_TO_BEARISH:
            band = _FLIP_TO_BEARISH[band.value]
        elif text_dir == "bullish" and band.value in _FLIP_TO_BULLISH:
            band = _FLIP_TO_BULLISH[band.value]
        # 地缘风险校正：含明确地缘风险/冲突信号且 LLM 误标 bullish（无受益涨幅词）→ 翻 bearish
        _dict_risk_text = str(d.get("title", "")) + " " + str(d.get("impact_reason", ""))
        if _has_geopolitical_risk(_dict_risk_text) and band.value in _FLIP_TO_BEARISH:
            band = _FLIP_TO_BEARISH[band.value]
        # 低分方向性 band 强制中性：弱信号（sc<4）不带动市场情绪，无论 LLM 标 bullish/bearish 一律中性化
        _has_text = str(d.get("impact_reason", "")) or str(d.get("analysis_chain", ""))
        if score < LOW_SCORE_NEUTRAL_THRESHOLD \
                and band not in (ImpactBand.NEUTRAL, ImpactBand.MIXED) \
                and _has_text:
            band = ImpactBand.NEUTRAL
        d["impact_band"] = band.value
        d["sentiment"] = band.value
        d["impact_direction"] = _band_to_direction(band)
        if is_model:
            item.impact_band = band
            item.sentiment = band.value
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
        extra_body["reasoning_effort"] = "low"  # 降低推理开销，缩短 Agnes 响应耗时（实测单次 30~65s）
    # OpenRouter 官方要求 HTTP-Referer 和 X-Title 请求头
    default_headers = {}
    if IS_OPENROUTER_OFFICIAL:
        default_headers = {
            "HTTP-Referer": "https://github.com/stock-news-agent",
            "X-Title": "StockNewsAgent",
        }
    return ChatOpenAI(
        model=OPENROUTER_MODEL_NAME,
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
        temperature=0.1,  # 结构化输出场景降低温度提升一致性
        max_tokens=16384,
        extra_body=extra_body,
        default_headers=default_headers,
        timeout=90,  # 与方式B一致；方式A不重试(max_retries=0)，超时后快速降级到方式B
        max_retries=0,  # 禁用 LangChain 内部重试——其 2 次重试 × 120s = 360s 会直接突破 llm_filter 的 300s 总 deadline
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
        news_list=json.dumps(truncated_batch, ensure_ascii=False, separators=(",", ":"))
    )


def _build_simple_prompt(news_batch: list) -> str:
    """构建简化版分析提示词（JSON解析失败时重试用）

    去掉 analysis_chain / influence_scope / confidence 等复杂字段，
    只保留核心分析字段，降低推理模型的输出复杂度，提高 JSON 合规率。
    """
    truncated_batch = []
    for n in news_batch:
        item = dict(n)
        content = item.get("content", "")
        if len(content) > 200:
            item["content"] = content[:200] + "..."
        truncated_batch.append(item)

    simple_prompt = """你是资深A股资讯分析师。对以下资讯逐条分析，输出简洁JSON。

## 资讯列表（共{n}条）
{news_list}

## 输出格式（每条只输出以下字段）
```json
{{
  "filtered_news": [
    {{
      "idx": 0,
      "title": "原标题",
      "market_impact_score": 7,
      "impact_band": "bullish",
      "affected_sectors": ["半导体"],
      "affected_stocks": ["中芯国际"],
      "impact_reason": "一句话影响逻辑",
      "influence_scope": "sector"
    }}
  ],
  "removed_count": 0
}}
```

band取值（多空方向，与 score 独立，禁止按 score 区间反推）: bullish(强利好) / mildly_bullish(弱利好) / neutral(中性) / mixed(多空交织) / mildly_bearish(弱利空) / bearish(强利空)
* score=影响强度/重要性(0-10)，band=多空方向，两者独立。重大利空（板块暴跌/制裁升级/爆雷）影响极大应给高分(7-9)且 band=bearish，不可因利空打低分。
influence_scope取值: market(影响全球/全市场宏观事件) / sector(影响整个板块) / stock(仅影响个股自身)

**每条必须回显 idx 字段**，值与输入编号完全一致。
只返回JSON，不要代码块包裹。"""
    return simple_prompt.format(
        n=len(truncated_batch),
        news_list=json.dumps(truncated_batch, ensure_ascii=False, separators=(",", ":"))
    )


def _clean_analysis_chain(chain: str) -> str:
    """统一 analysis_chain 格式：去除 LLM 偶发返回的步骤前缀标签

    推送实证：部分批次 LLM 返回 "事件识别:xxx→影响范围:板块级→方向:强利好→..."
    而非 prompt 要求的简洁箭头链。统一去除前缀让推送格式一致。
    """
    if not chain:
        return chain
    return re.sub(r'(事件识别|影响范围|方向|强度|置信度|置信|第\d步)[：:]\s*', '', chain).strip()


def _normalize_llm_item(raw: dict) -> dict | None:
    """将 LLM 返回的原始 dict 标准化为 NewsAnalysisItem 兼容格式

    容错处理：
    - 字段名变体（band/impact_band, score/market_impact_score 等）自动映射
    - 必填字段缺失时用规则推断补全，而不是整条丢弃
    - 返回 None 表示完全无法识别（连 title 都没有）
    """
    if not isinstance(raw, dict):
        return None

    # 字段别名映射（LLM 可能输出不同的字段名）
    _field_aliases = {
        "title": ["title", "标题", "news_title"],
        "idx": ["idx", "编号", "index", "序号", "_idx"],
        "market_impact_score": ["market_impact_score", "score", "impact_score", "评分", "影响力评分"],
        "impact_band": ["impact_band", "band", "档位", "方向", "sentiment_band"],
        "confidence": ["confidence", "置信度", "confidence_level"],
        "affected_sectors": ["affected_sectors", "sectors", "板块", "影响板块", "related_sectors"],
        "affected_stocks": ["affected_stocks", "stocks", "个股", "影响个股", "related_stocks"],
        "impact_reason": ["impact_reason", "reason", "原因", "逻辑", "分析理由"],
        "influence_scope": ["influence_scope", "scope", "影响范围", "level"],
        "analysis_chain": ["analysis_chain", "推理链", "reasoning", "chain"],
        "sentiment": ["sentiment", "情绪", "情绪方向"],
        "source": ["source", "来源"],
        "content": ["content", "内容", "摘要"],
        "published_at": ["published_at", "publish_time", "发布时间", "time"],
        "category": ["category", "分类", "类别"],
    }

    normalized = {}
    for std_field, aliases in _field_aliases.items():
        for alias in aliases:
            if alias in raw and raw[alias] is not None:
                normalized[std_field] = raw[alias]
                break

    # title 是唯一硬性要求——没有 title 无法匹配原始新闻
    if not normalized.get("title"):
        return None

    # score 缺失 → 默认 5.0（中性）
    try:
        score = float(normalized.get("market_impact_score", 5.0))
        score = max(0.0, min(10.0, score))
    except (ValueError, TypeError):
        score = 5.0
    normalized["market_impact_score"] = score

    # impact_band 缺失/非法 → 默认 neutral（score 只表影响强度，推不出多空方向）
    if not normalized.get("impact_band"):
        normalized["impact_band"] = ImpactBand.NEUTRAL.value
    else:
        # 规范化 band 值
        band_str = str(normalized["impact_band"]).lower().strip()
        try:
            normalized["impact_band"] = ImpactBand(band_str).value
        except ValueError:
            # 无法识别的 band → 中性（不再用 score 反推方向）
            normalized["impact_band"] = ImpactBand.NEUTRAL.value

    # confidence 缺失 → 默认 medium
    if not normalized.get("confidence"):
        normalized["confidence"] = "medium"
    else:
        conf_str = str(normalized["confidence"]).lower().strip()
        if conf_str not in ("high", "medium", "low"):
            normalized["confidence"] = "medium"

    # 列表字段兜底
    for list_field in ("affected_sectors", "affected_stocks"):
        if list_field not in normalized or normalized[list_field] is None:
            normalized[list_field] = []
        elif isinstance(normalized[list_field], str):
            # 字符串形式的列表，按逗号/顿号分隔
            normalized[list_field] = [
                s.strip() for s in normalized[list_field].replace("、", ",").split(",")
                if s.strip()
            ]

    # 字符串字段兜底
    for str_field in ("impact_reason", "influence_scope", "analysis_chain", "sentiment",
                      "source", "content", "published_at", "category"):
        if str_field not in normalized or normalized[str_field] is None:
            normalized[str_field] = ""

    # analysis_chain 格式统一：去除 LLM 偶发返回的步骤前缀（推送实证格式不一致）
    if normalized.get("analysis_chain"):
        normalized["analysis_chain"] = _clean_analysis_chain(normalized["analysis_chain"])

    # sentiment 与 band 对齐
    if not normalized["sentiment"]:
        normalized["sentiment"] = normalized["impact_band"]

    # idx 归整为 int（LLM 可能以字符串形式回显编号）
    if "idx" in normalized:
        try:
            normalized["idx"] = int(normalized["idx"])
        except (ValueError, TypeError):
            normalized.pop("idx", None)

    return normalized


def _parse_llm_items(content: str) -> list:
    """解析 LLM 返回内容为标准化 dict 列表（容错版）

    不再用 NewsAnalysisItem(**raw) 硬校验——字段缺失或命名有偏差时，
    通过 _normalize_llm_item 推断补全，避免因单条格式问题导致整个批次被判定为解析失败。
    """
    parsed = _safe_parse_json(content)
    raw_items = parsed.get("filtered_news", [])
    items = []
    for raw in raw_items:
        normalized = _normalize_llm_item(raw)
        if normalized:
            items.append(normalized)
    return items


def _llm_analyze_batch_structured(batch: list, deadline: float = 0) -> list:
    """结构化输出 + 自由文本降级 + 简化prompt重试

    三级调用策略：
      方式A: LangChain ChatOpenAI（timeout=120s）
      方式B: requests 直调 _call_llm_api（timeout=90s, max_retries=2）
      方式C: 简化prompt重试（去掉 analysis_chain 等复杂字段，降低输出复杂度）

    deadline: 总超时熔断时间戳(time.monotonic)，0=不限。
              每级调用前检查，且 _call_llm_api 内部重试也受 deadline 约束，
              超时立即抛出，避免单批 A→B→C 最坏 >480s 突破 llm_filter_node 的 300s 总超时。
    全部失败时抛异常（不静默返回原始数据），让 llm_filter_node 的重试机制生效。
    """
    prompt = _build_analysis_prompt(batch)
    system_msg = "你是资深A股资讯分析师。请直接返回纯JSON，不要使用```json代码块包裹，不要在JSON字符串中使用换行符。"

    # 方式A：LangChain 直接调用 + _safe_parse_json
    if deadline and time.monotonic() >= deadline:
        raise Exception("LLM 总超时熔断，跳过方式A")
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = _build_llm()
        resp = llm.invoke([SystemMessage(content=system_msg), HumanMessage(content=prompt)])
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        items = _parse_llm_items(content)
        if items:
            return _apply_guardrails(items)
        logger.warning(f"方式A解析为空，原始前200字: {content[:200]}")
    except Exception as e:
        logger.warning(f"方式A(LLM直接调用)失败: {e}")

    # 方式B：requests 直调 + _safe_parse_json（1次重试，应对瞬时网络/限流问题）
    if deadline and time.monotonic() >= deadline:
        raise Exception("LLM 总超时熔断，跳过方式B")
    try:
        content = _call_llm_api(system_msg, prompt, timeout=90, max_retries=1, deadline=deadline)
        items = _parse_llm_items(content)
        if items:
            return _apply_guardrails(items)
        logger.warning(f"方式B解析为空，原始前200字: {content[:200]}")
    except Exception as e:
        logger.warning(f"方式B(requests直调)失败: {e}")

    # 方式C：简化prompt重试（降低输出复杂度，提高JSON合规率）
    if deadline and time.monotonic() >= deadline:
        raise Exception("LLM 总超时熔断，跳过方式C")
    try:
        simple_prompt = _build_simple_prompt(batch)
        simple_sys = "你是资深A股资讯分析师。直接返回简洁JSON，不要代码块包裹。"
        content = _call_llm_api(simple_sys, simple_prompt, timeout=90, max_retries=1, deadline=deadline)
        items = _parse_llm_items(content)
        if items:
            logger.info(f"方式C(简化prompt)成功恢复{len(items)}条")
            return _apply_guardrails(items)
        logger.warning(f"方式C解析为空，原始前200字: {content[:200]}")
    except Exception as e:
        logger.warning(f"方式C(简化prompt)失败: {e}")

    # 全部失败：抛异常让 llm_filter_node 重试机制生效（不再静默返回原始数据）
    raise Exception("LLM三级调用全部失败(方式A/B/C)，需上层重试或降级")


# ============================================================
# 节点2: prefilter - Python预过滤
# ============================================================

def prefilter_node(state: AgentState) -> dict:
    """Stage 1 (Python) 预过滤: 关键词去噪 + 去重 + 重要度初筛 -> top 20
    
    结果写入 prefiltered_news
    """
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

# HIGH_SIGNAL_KEYWORDS 从 src.tools.keyword_tables 导入（共享单一事实来源）

# 预编译信号关键词正则：合并 HIGH_SIGNAL_KEYWORDS + TECH_HARDWARE_KEYWORDS，
# 用 re.escape 转义 "*ST" 等含正则元字符的关键词，
# 正则引擎一次扫描即可匹配全部关键词，替代逐词 `in` 遍历（O(n×kw_count) → O(n)）
_ALL_SIGNAL_KEYWORDS = HIGH_SIGNAL_KEYWORDS + TECH_HARDWARE_KEYWORDS


def _signal_kw_pattern(kw: str) -> str:
    """信号关键词 → 正则片段：英文缩写加词边界（nAMD 不触发 AMD 信号）"""
    if kw in _TECH_ENGLISH_WORDS:
        return rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])"
    return re.escape(kw)


_SIGNAL_PATTERN = re.compile("|".join(_signal_kw_pattern(kw) for kw in _ALL_SIGNAL_KEYWORDS))


def _has_high_signal(news_list: list) -> bool:
    """检测资讯列表中是否存在任何重磅信号关键词 (含科技硬件)

    性能优化: 预编译正则 _SIGNAL_PATTERN 一次扫描所有关键词，
    替代原先对每条 news 遍历两个关键词列表的逐词 `in` 检查。
    """
    for news in news_list:
        title = news.get("title", "")
        content = news.get("content", "")
        name = news.get("name", "")
        text = f"{title} {content}"
        if name:
            text = text.replace(name, "")
        if _SIGNAL_PATTERN.search(text):
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
    # 创建浅拷贝，避免直接修改 state 中的 prefiltered_news 字典（LangGraph 节点应返回 partial update）
    prefiltered = [dict(n) for n in state.get("prefiltered_news", [])]
    if not prefiltered:
        return {
            "filtered_news": [],
            "messages": [AIMessage(content="[llm_filter] 无有效资讯需分析")]
        }

    logger.info(f"[llm_filter] 预筛{len(prefiltered)}条，准备全部进行深度分析")

    # 并发批量调用 LLM
    all_llm_results = []
    total_llm_fallback = 0  # LLM 未分析(截断/判噪)而走规则降级保留的条数
    batch_errors = 0
    error_details = []

    # 当前统一使用 Agnes 端点(agnes-2.5-flash)：串行调用避免触发限流，
    # 10条/批、批次间隔 2s。早期"中转平台(qwqtao)"模型已过期废弃，不再保留慢速分支。
    BATCH_SIZE = 10
    batches = [prefiltered[i:i + BATCH_SIZE] for i in range(0, len(prefiltered), BATCH_SIZE)]

    # 给每条预筛条目分配稳定 idx（注入 news 对象，随 prompt 序列化传给 LLM）。
    # LLM 回显 idx 后按 idx 精确 merge，避免标题被改写/截断/合并导致分析被整体丢弃
    # （实跑实证：60 输入→LLM 解析 55 条→仅 26 条标题精确匹配，34 条走规则兜底）。
    for idx, news in enumerate(prefiltered):
        news["idx"] = idx

    # 注: 不再使用 socket.setdefaulttimeout(None)——LLM 调用均通过 requests/httpx
    # 显式 timeout 参数控制,全局 setdefaulttimeout 会污染 FastAPI 线程池其他请求。

    try:
        # 串行调用（避免并发触发 Agnes 端点限流）
        rate_limit_interval = 2
        # 总超时熔断：40条/4批 × (90s+重试) Agnes 端点理论最坏 360s+，
        # 设 deadline=300s 超时后剩余批次降级为规则分析，避免云端 cron 超杀
        _LLM_TOTAL_DEADLINE = 300
        deadline = time.monotonic() + _LLM_TOTAL_DEADLINE
        results_map = {}
        for idx, batch in enumerate(batches):
            if time.monotonic() >= deadline:
                logger.warning(f"LLM 总超时熔断({_LLM_TOTAL_DEADLINE}s): 批次{idx}及后续共{len(batches)-idx}批降级")
                batch_errors += len(batches) - idx
                error_details.append(f"批次{idx}~{len(batches)-1}(总超时降级)")
                break
            try:
                results_map[idx] = _llm_analyze_batch_structured(batch, deadline=deadline)
                logger.info(f"LLM 批次 {idx} 分析完成")
            except Exception as e:
                # 首次失败后重试一次（超时/限流等瞬时错误常可通过重试解决）
                # 重试前检查 deadline，避免重试本身突破总超时
                if time.monotonic() >= deadline:
                    logger.warning(f"LLM 批次 {idx} 失败且已达总超时，跳过重试降级")
                    results_map[idx] = None
                    batch_errors += 1
                    error_details.append(f"批次{idx}(总超时降级)")
                    continue
                logger.warning(f"LLM 批次 {idx} 首次失败: {e}, 5s后重试...")
                time.sleep(5)
                try:
                    results_map[idx] = _llm_analyze_batch_structured(batch, deadline=deadline)
                    logger.info(f"LLM 批次 {idx} 重试成功")
                except Exception as e2:
                    results_map[idx] = None
                    batch_errors += 1
                    error_details.append(f"批次{idx}(重试仍失败): {str(e2)[:80]}")
                    logger.warning(f"LLM 批次 {idx} 重试仍失败: {e2}")
            # 批次间限流间隔（最后一批不需要等）
            if idx < len(batches) - 1 and rate_limit_interval > 0:
                logger.info(f"限流等待 {rate_limit_interval}s（Agnes 端点批次间保护间隔，避免长时间空跑被网关掐断）...")
                time.sleep(rate_limit_interval)

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
                # 失败降级处理：对原始news补全方向/板块/band字段，
                # 避免下游rank_news用默认neutral/3.0导致排名偏低
                for n in batch:
                    if n.get("category") == "signal":
                        # 信号情报已有方向，直接保留
                        all_llm_results.append(n)
                        continue
                    direction = predict_direction_by_rules(n.get("title", ""), n.get("content", ""))
                    n["market_impact_score"] = 5.0 if direction != "neutral" else 3.0
                    n["impact_direction"] = direction
                    n["affected_sectors"] = infer_sectors_by_rules(
                        n.get("title", ""), n.get("content", ""), n.get("name", ""))
                    n["impact_reason"] = "大模型调用降级：基于规则系统自动分析"
                    n["sentiment"] = direction
                    if direction == "bullish":
                        n["impact_band"] = "mildly_bullish"
                    elif direction == "bearish":
                        n["impact_band"] = "mildly_bearish"
                    else:
                        n["impact_band"] = "neutral"
                    n["confidence"] = "low"
                    n["influence_scope"] = ""  # 空值让 rank_news 的 _infer_influence_scope 推断，避免误沉底
                    n["analysis_chain"] = ""
                    all_llm_results.append(n)

    except Exception as e:
        logger.error(f"[llm_filter] LLM 并发流异常: {e}", exc_info=True)
        # 全部降级
        all_llm_results = prefiltered

    # 3. 结果合并: 使用 LLM 结果中的 impact 字段更新原始 news 对象, 保留原始所有的属性(解决丢失问题)
    final_filtered = []
    # 用 (title, published_at) 复合 key 避免同标题碰撞
    llm_results_by_key = {}
    for n in all_llm_results:
        key = (n.get("title", "").strip(), str(n.get("published_at", "")))
        llm_results_by_key[key] = n
    # 同时保留 title-only 索引作为兜底（LLM 可能截断 published_at）
    llm_results_by_title = {n.get("title", "").strip(): n for n in all_llm_results}
    # 按 idx 精确索引（LLM 回显的编号）——merge 主匹配键，彻底解决标题改写导致的不匹配
    llm_results_by_idx = {}
    for n in all_llm_results:
        _iv = n.get("idx")
        if _iv is not None:
            try:
                llm_results_by_idx[int(_iv)] = n
            except (ValueError, TypeError):
                pass

    for news in prefiltered:
        title = news.get("title", "").strip()
        pub = str(news.get("published_at", ""))
        # 匹配优先级：idx（LLM 回显编号）> 复合 key > title 兜底
        # 只要 LLM 按要求回显 idx，即可 100% 命中，57% 分析被丢弃的问题不再发生
        llm_res = None
        _my_idx = news.get("idx")
        if _my_idx is not None:
            try:
                llm_res = llm_results_by_idx.get(int(_my_idx))
            except (ValueError, TypeError):
                llm_res = None
        if llm_res is None:
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
                # band 与 direction 同步：避免降级时利好资讯被排到最后
                if direction == "bullish":
                    news["impact_band"] = "mildly_bullish"
                elif direction == "bearish":
                    news["impact_band"] = "mildly_bearish"
                else:
                    news["impact_band"] = "neutral"
                news["confidence"] = "low"
                news["influence_scope"] = ""  # 空值让 rank_news 推断，避免宏观资讯误沉底
                news["analysis_chain"] = ""
            else:
                # 2. 正常 LLM 输出: 尊重 LLM 方向, 但对中性结论做精细纠偏
                #    LLM 推理链为标题方向的最终权威，规则仅加分不改方向
                if direction == "neutral":
                    rule_dir = predict_direction_by_rules(news.get("title", ""), news.get("content", ""))
                    if rule_dir != "neutral":
                        # 规则检测到信号：仅提升排名分数，不覆写 LLM 判定的方向
                        # 标题标签(利好/利空)以 LLM 推理链为准，最终一致性校验统一对齐
                        orig_score = llm_res.get("market_impact_score", 3.0)
                        try:
                            orig_score = float(orig_score)
                        except Exception:
                            orig_score = 3.0
                        news["market_impact_score"] = min(orig_score + 1.0, 10.0)
                        llm_reason = llm_res.get("impact_reason", "")
                        news["impact_reason"] = (llm_reason + " | 规则补充: 检测到多空信号词").strip(" |")
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
                # band-direction 同步交由最终一致性校验统一处理，此处不再二次纠偏
                # （原中间纠偏与 guardrails + final check 三重执行，阈值不同导致 flip-flop）
                news["confidence"] = conf_val.value if hasattr(conf_val, "value") else str(conf_val)
                news["influence_scope"] = llm_res.get("influence_scope", "")  # 空值让 rank_news 推断
                news["analysis_chain"] = llm_res.get("analysis_chain", "")

            final_filtered.append(news)
        else:
            # 该条目在 LLM 结果中找不到匹配——可能是 LLM 判定为噪音排除，
            # 也可能是输出被 max_tokens 截断导致丢失（日志证实常态发生）。
            # 统一按"未分析"走规则降级保留，避免截断丢失被误计为噪音静默丢弃。
            direction = predict_direction_by_rules(news.get("title", ""), news.get("content", ""))
            news["market_impact_score"] = 5.0 if direction != "neutral" else 3.0
            news["impact_direction"] = direction
            news["affected_sectors"] = infer_sectors_by_rules(
                news.get("title", ""), news.get("content", ""), news.get("name", ""))
            news["affected_stocks"] = news.get("affected_stocks", [])
            news["impact_reason"] = "LLM未返回分析结果，基于规则系统自动分析"
            news["sentiment"] = direction
            if direction == "bullish":
                news["impact_band"] = "mildly_bullish"
            elif direction == "bearish":
                news["impact_band"] = "mildly_bearish"
            else:
                news["impact_band"] = "neutral"
            news["confidence"] = "low"
            news["influence_scope"] = ""  # 空值让 rank_news 推断，避免宏观资讯误沉底
            news["analysis_chain"] = ""
            final_filtered.append(news)
            total_llm_fallback += 1  # LLM 未分析，已按规则降级保留

    # 4. 最终一致性校验：band 方向 vs reason/chain 文本方向
    #    合并过程中多次修改 band/direction/reason，可能产生最终不一致
    #    （如 band=bullish 但 reason 写"利空"），此处做最后一道校正
    _FLIP_TO_BEARISH_FINAL = {"bullish": "bearish", "mildly_bullish": "mildly_bearish", "mixed": "mildly_bearish"}
    _FLIP_TO_BULLISH_FINAL = {"bearish": "bullish", "mildly_bearish": "mildly_bullish", "mixed": "mildly_bullish"}
    consistency_fixes = 0
    for news in final_filtered:
        # 信号情报方向由交易所官方数据锁定，不做文本方向翻转
        if news.get("category") == "signal":
            continue
        band = str(news.get("impact_band", "neutral"))
        reason = str(news.get("impact_reason", ""))
        chain = str(news.get("analysis_chain", ""))
        # 显式中性标记强制中性（最终安全网）：LLM 写明"无明确多空信号/无实质影响"等，
        # 即使 band 标了方向性也必须判中性，盖过"回购"等关键词误触发的 bullish 检测
        if _has_explicit_neutral_marker(reason + " " + chain):
            if band != "neutral":
                news["impact_band"] = "neutral"
                news["impact_direction"] = "neutral"
                news["sentiment"] = "neutral"
                consistency_fixes += 1
            continue
        text_dir = _infer_direction_from_text(reason, chain)
        if text_dir == "bearish" and band in _FLIP_TO_BEARISH_FINAL:
            news["impact_band"] = _FLIP_TO_BEARISH_FINAL[band]
            news["impact_direction"] = "bearish"
            news["sentiment"] = news["impact_band"]
            consistency_fixes += 1
        elif text_dir == "bullish" and band in _FLIP_TO_BULLISH_FINAL:
            news["impact_band"] = _FLIP_TO_BULLISH_FINAL[band]
            news["impact_direction"] = "bullish"
            news["sentiment"] = news["impact_band"]
            consistency_fixes += 1
        # 中性文本 + 低分不应标方向性 band（推送实证：常规公告被标"强利空"）
        try:
            _score = float(news.get("market_impact_score", 3.0))
        except (ValueError, TypeError):
            _score = 3.0
        # 读取最新 band（经上方 text_dir 翻转后可能已变更）
        _cur_band = news.get("impact_band", "neutral")
        # 低分方向性 band 强制中性：弱信号（sc<4）不带动市场情绪，无论 LLM 标 bullish/bearish 一律中性化
        if _score < LOW_SCORE_NEUTRAL_THRESHOLD \
                and _cur_band not in ("neutral", "mixed") \
                and (reason or chain):
            news["impact_band"] = "neutral"
            news["impact_direction"] = "neutral"
            news["sentiment"] = "neutral"
            consistency_fixes += 1
            _cur_band = "neutral"
        # 地缘风险校正：含明确地缘风险/冲突信号且 LLM 误标 bullish（无受益涨幅词）→ 翻 bearish。
        # 必须在低分中性之后（低分已中性化的弱风险不再翻，高 sc 地缘风险仍纠正）。
        _final_risk_text = str(news.get("title", "")) + " " + reason
        if _has_geopolitical_risk(_final_risk_text) and _cur_band in ("bullish", "mildly_bullish"):
            news["impact_band"] = "bearish" if _cur_band == "bullish" else "mildly_bearish"
            news["impact_direction"] = "bearish"
            news["sentiment"] = news["impact_band"]
            consistency_fixes += 1
            _cur_band = news["impact_band"]
        # band-direction 最终同步：确保 impact_direction 与 impact_band 方向一致
        # （替代已移除的中间纠偏 pass2，统一在此处一次性处理，避免 flip-flop）
        _final_dir = str(news.get("impact_direction", "neutral"))
        if _final_dir == "bullish" and "bullish" not in _cur_band and _cur_band not in ("neutral", "mixed"):
            news["impact_band"] = "mildly_bullish"
            news["sentiment"] = "mildly_bullish"
            consistency_fixes += 1
        elif _final_dir == "bearish" and "bearish" not in _cur_band and _cur_band not in ("neutral", "mixed"):
            news["impact_band"] = "mildly_bearish"
            news["sentiment"] = "mildly_bearish"
            consistency_fixes += 1
    if consistency_fixes:
        logger.info(f"[llm_filter] 最终一致性校验: 修正{consistency_fixes}条 band↔文本方向冲突")

    # 5. 生成返回状态
    msg = (
        f"[llm_filter] 完成：深度分析{len(prefiltered)}条，"
        f"保留{len(final_filtered)}条"
        + (f"，其中{total_llm_fallback}条LLM未分析走规则降级" if total_llm_fallback else "")
        + "。"
    )
    if batch_errors:
        msg += f" | {batch_errors}批降级 | 错误: {'; '.join(error_details[:2])}"

    # 清理临时匹配键 idx，避免泄漏到下游/推送/前端
    for n in final_filtered:
        n.pop("idx", None)

    return {
        "filtered_news": final_filtered,
        "messages": [AIMessage(content=msg)]
    }


# ============================================================
# 节点3: rank_news - Python评分排名 + LLM智能重排
# ============================================================

SCORE_ADJUST_PROMPT = """你是资深A股投研总监。以下资讯已经过初步筛选和评分，请对每条资讯的重要性分数进行调整。

## 资讯列表（共{n}条，按初步评分排序）
{news_list}

## 调整原则
你只需调整每条资讯的 adjusted_score（影响分，0-10），**不要直接排序**。
最终排序由系统根据调整后的综合分自动计算。调整时请考虑：
1. 对A股科技板块的实质影响程度：能带动市场/板块情绪的重大资讯应高分
2. 影响范围：市场级(美联储/地缘/央行全面政策) > 板块级(行业政策/龙头带动) > 个股级
3. 信号强度与时效性：重大突发(当日)优先于常规资讯(前日)
4. 信息确定性：官方公告/多源报道 > 市场传闻/单一来源
5. 利好利空均可高分，关键是影响程度；纯噪音应低分

## 输出格式
只返回一个JSON对象，不要代码块包裹、不要任何前后缀文字:
```json
{{
  "adjustments": [
    {{"title": "原标题", "adjusted_score": 8.5}}
  ]
}}
```

注意:
- adjusted_score 范围 0-10，参考 init_score 上下调整。数量与输入一致（共{n}条）。
- title 必须与输入中的 title 完全一致（一字不差），否则该条调整将无法匹配。
- 输出必须是一个完整的JSON，严禁截断；若内容过长请优先精简 title 外的所有描述，绝不省略任何一条。
- 只输出上述 JSON，不输出分析过程、总结或任何其他文字。
"""


def _llm_adjust_scores(ranked_news: list, top_n: int = 20, deadline: float = 0) -> list:
    """LLM 调整 market_impact_score，仅在同 scope 同 band 内重排

    LLM 仅调整影响分权重（market_impact_score），不跨 scope/band 重排。
    调整后重新计算 total_score，在同一 (scope, band) 组内按 total_score 降序重排，
    组间保持 Python 原始排序顺序（scope > band 主序不被破坏）。

    Args:
        ranked_news: Python 初步排名结果
        top_n: 取前 N 条让 LLM 调整评分

    Returns:
        调整评分后的重排列表（若 LLM 失败则返回原始排名）
    """
    if len(ranked_news) <= 3:
        return ranked_news

    candidates = ranked_news[:top_n]
    tail = ranked_news[top_n:]

    # 构建精简资讯列表（只保留评分调整所需字段）
    slim_list = []
    for i, n in enumerate(candidates, 1):
        slim_list.append({
            "init_rank": i,
            "title": n.get("title", ""),
            "impact_band": n.get("impact_band", ""),
            "init_score": n.get("market_impact_score", 0),
            "influence_scope": n.get("influence_scope", ""),
            "impact_direction": n.get("impact_direction", ""),
            "confidence": n.get("confidence", ""),
            "published_at": n.get("published_at", ""),
            "impact_reason": n.get("impact_reason", ""),
        })

    prompt = SCORE_ADJUST_PROMPT.format(
        n=len(slim_list),
        news_list=json.dumps(slim_list, ensure_ascii=False, separators=(",", ":"))
    )
    system_msg = "你是资深A股投研总监。请直接返回纯JSON，不要使用代码块包裹。"

    try:
        content = _call_llm_api(system_msg, prompt, timeout=90, max_retries=1, deadline=deadline)
        parsed = _safe_parse_json(content)
        adjustments = parsed.get("adjustments", [])
        if not adjustments:
            logger.warning("[llm_adjust] LLM返回空调整，使用原始排名")
            return ranked_news

        # 构建 title -> adjusted_score 映射
        title_to_score = {}
        for item in adjustments:
            title = item.get("title", "").strip()
            score = item.get("adjusted_score", 0)
            if title and score is not None:
                try:
                    title_to_score[title] = max(0.0, min(10.0, float(score)))
                except (ValueError, TypeError):
                    pass

        # 更新 market_impact_score
        adjusted_count = 0
        for n in candidates:
            t = n.get("title", "").strip()
            if t in title_to_score:
                n["market_impact_score"] = title_to_score[t]
                adjusted_count += 1
        logger.info(f"[llm_adjust] LLM调整{adjusted_count}/{len(candidates)}条评分")

        # 重新计算 total_score（复用 _calc_continuous_score + confidence 加权）
        from src.tools.data_fetchers import get_hs300_constituents
        from src.tools.calculators import (
            _calc_continuous_score, CONFIDENCE_WEIGHT, BAND_PRIORITY, SCOPE_SCORE_BOOST,
        )
        hs300 = get_hs300_constituents()

        for n in candidates:
            total = _calc_continuous_score(n, hs300)
            conf = n.get("confidence", "medium")
            total = round(total * CONFIDENCE_WEIGHT.get(conf, 0.85), 4)
            n["total_score"] = total

        # 按 (band, scope) 分组，组内按 total_score 重排，组间保持 Python 原始顺序
        # LLM 只能在同 band 同 scope 内微调，不跨档重排
        # 注: 原实现用 itertools.groupby——它要求序列已按键连续排序，而 candidates
        # 的排序键是 (band_priority, total_score+scope加成)，同 (band, scope) 并不保证
        # 相邻，groupby 会把同一分组拆成多段，组间顺序失真。改用 dict 分组。
        groups = {}
        for c in candidates:
            groups.setdefault(
                (c.get("impact_band", ""), c.get("influence_scope", "")), []
            ).append(c)
        reranked_candidates = []
        for group_list in groups.values():
            group_list.sort(
                key=lambda x: (x.get("total_score", 0), x.get("time_factor", 1.0)),
                reverse=True
            )
            reranked_candidates.extend(group_list)

        return reranked_candidates + tail
    except Exception as e:
        logger.warning(f"[llm_adjust] LLM调整失败，使用原始排名: {e}")
        return ranked_news


def rank_news_node(state: AgentState) -> dict:
    """Python评分排名 + LLM智能重排"""
    filtered_news = state.get("filtered_news", [])
    if not filtered_news:
        filtered_news = state.get("prefiltered_news", [])

    # Stage 1: Python 初步排名
    ranked = rank_news(filtered_news)

    # Stage 2: LLM 调整影响分权重，再按综合分重排（LLM 不直接排序，仅调整 market_impact_score）
    if ranked:
        ranked = _llm_adjust_scores(ranked, top_n=20, deadline=time.monotonic() + 120)

    # Stage 3: 展示层统一去重（同事件去重 + 同股公告限额）
    # 与微信推送共用 dedup_and_cap_for_display，保证 Web UI 与推送两端一致，
    # 避免"推送已去重但 UI 仍有重复"（寒武纪单日 9 条公告实证）
    ranked = dedup_and_cap_for_display(ranked)

    top_score = ranked[0]["total_score"] if ranked else 0

    # 如果最终结果为空，提供明确的用户反馈
    msg_parts = [f"[rank_news] 排名完成: 共{len(ranked)}条, 最高分: {top_score:.4f} (Python排名+LLM调分)"]
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

