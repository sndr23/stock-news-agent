# filepath: src/tools/calculators.py
"""
A股资讯业务计算工具

最终排名评分 (rank_news):
  total = (w_cred × 可信度 + w_imp × LLM重要度 [+ w_cluster × 聚类热度]) × 方向折扣 × 科技加成
  - 权重按类别分配, 各类加和均为 1.0:
      新闻: 可信度0.15 + LLM重要度0.70 + 聚类热度0.15
      公告: 可信度0.05 + LLM重要度0.95
      信号: 可信度0.10 + LLM重要度0.90
  - 方向折扣: 非中性=1.0; 中性按 LLM 重要度分级 (高分不折/中分轻折/低分重折)
  - 科技加成: 科技资讯统一×1.20; 非科技非国家级×0.85; 市场级豁免降权; 不封顶
  - time_factor 纳入主评分(小幅): total × (0.90 + 0.10×tf), 最新×1.00 ~ 更早×0.96

预筛重要度 (calculate_prefilter_importance): 多因子叠加 + Sigmoid 压缩, 仅用于预过滤阶段
方向兜底 (predict_direction_by_rules): 强正向组合优先, 避免利空短词误判
"""
from typing import TypedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import logging
import math
import re

logger = logging.getLogger(__name__)

# 关注股票列表缓存
_watchlist_cache = None


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
    influence_scope: str
    analysis_chain: str


# ============================================================
# 可信度评分 (0.30-0.99)
# ============================================================

CREDIBILITY_MAP = {
    # 交易所官方 (0.97-0.99)
    "上海证券交易所": 0.99, "深圳证券交易所": 0.99, "北京证券交易所": 0.98, "北交所": 0.98,
    # 国家级官方媒体 (0.93-0.96)
    "央视新闻": 0.96, "新华社": 0.96, "人民日报": 0.95, "经济日报": 0.94, "光明日报": 0.93,
    # 证监会/央行 (0.95-0.97)
    "证监会": 0.97, "央行": 0.97, "国务院": 0.97, "银保监会": 0.96, "国家发改委": 0.96,
    # 三大证券报 (0.90-0.92)
    "中国证券报": 0.92, "上海证券报": 0.91, "证券时报": 0.90, "证券日报": 0.90,
    # 财联社/第一财经 (0.86-0.89)
    "财联社电报": 0.89, "财联社": 0.89, "第一财经": 0.87,
    # 交易所信号 (官方披露, 可信度极高)
    "交易所龙虎榜": 0.98, "交易所业绩预告": 0.98,
    # 同花顺/新浪 (0.80-0.84)
    "同花顺快讯": 0.84, "新浪财经": 0.82,
    # 主流财经门户 (0.80-0.85)
    "东方财富快讯": 0.84, "东方财富": 0.83, "同花顺": 0.81,
    "和讯网": 0.80, "金融界": 0.80, "金十数据": 0.80,
    # 国际权威通讯社 (0.85-0.88, Google News 抓取的路透/彭博等源)
    "Reuters": 0.88, "Bloomberg": 0.88, "Associated Press": 0.85,
    "Dow Jones": 0.85, "路透": 0.88, "彭博": 0.88, "美联社": 0.85,
    # 券商研报 (0.72-0.78)
    "中信证券": 0.78, "中金公司": 0.77, "海通证券": 0.75, "华泰证券": 0.74,
    "国泰君安": 0.74, "招商证券": 0.73, "广发证券": 0.72,
    # 行业媒体 (0.65-0.70)
    "21世纪经济报道": 0.70, "财新网": 0.69, "界面新闻": 0.67,
    # 自媒体/低可信 (0.30-0.45)
    "企业自媒体报道": 0.42, "股吧": 0.33, "雪球": 0.38,
}

DEFAULT_CREDIBILITY = 0.62


def calculate_credibility(source: str) -> float:
    """计算来源可信度 (0.30-0.99)"""
    if not source:
        return DEFAULT_CREDIBILITY
    if source in CREDIBILITY_MAP:
        return CREDIBILITY_MAP[source]
    for key, score in CREDIBILITY_MAP.items():
        if key in source or source in key:
            return score
    if any(k in source for k in ["交易所", "证监会", "央行", "国务院", "银保监", "发改委"]):
        return 0.95
    if any(k in source for k in ["证券报", "证券时报", "财经", "时报", "日报"]):
        return 0.82
    if any(k in source for k in ["券商", "证券", "研究"]):
        return 0.74
    return DEFAULT_CREDIBILITY


# ============================================================
# 重要度评分 — 多因子叠加模型
# ============================================================

HIGH_IMPACT_KEYWORDS = {
    "退市": 0.25, "立案调查": 0.25, "重大违法": 0.22,
    "破产重整": 0.25, "业绩暴雷": 0.22, "巨额亏损": 0.20,
    "债务违约": 0.20, "重大重组": 0.18, "借壳上市": 0.18,
    "并购": 0.15, "涨停": 0.12, "跌停": 0.12,
    "监管处罚": 0.15, "业绩超预期": 0.15,
    "业绩预增": 0.12, "业绩预减": 0.12,
    "ST": 0.10, "爆雷": 0.20, "跑路": 0.20,
    # 外围风险事件（对A股有传导效应）
    "熔断": 0.25, "暴跌": 0.20, "崩盘": 0.22,
    "债务危机": 0.20, "银行危机": 0.20, "金融风险": 0.18,
    "战争": 0.20, "军事冲突": 0.18, "冲突": 0.15, "制裁": 0.15,
    "地缘": 0.15, "关税": 0.15, "贸易战": 0.15,
    "出口管制": 0.15, "禁运": 0.18,
    "美联储": 0.15, "加息": 0.12, "降息": 0.12,
    "原油暴跌": 0.18, "原油暴涨": 0.18,
    "韩指": 0.18, "韩元": 0.15, "日元暴跌": 0.18,
    # 宏观数据发布（2026-08-12 审核实证：CPI 预筛分仅 0.14 被拦致美国7月CPI全漏推。
    # 打分兜底——即使标题未命中高信号词直通，含宏观数据词的也有机会过 0.55 门槛）
    "CPI": 0.20, "PPI": 0.18, "通胀": 0.15, "物价": 0.12,
    "利率决议": 0.18, "FOMC": 0.18, "GDP": 0.15, "失业率": 0.15,
    "就业数据": 0.15, "美债": 0.12, "社融": 0.15, "LPR": 0.15, "PMI": 0.15,
    # 全球流动性/套息/利率/汇率/资金面（量化资金驱动因子打分兜底，2026-08-14 审核实证：
    # 8-13 日本加息预期→日元套息平仓→A股尾盘跳水，但"日本央行/日元/套息"等词不在
    # 打分表，即使未命中高信号词直通，也应有机会过 0.55 门槛进入 LLM 判定）
    "日本央行": 0.18, "日银": 0.15, "植田": 0.15, "日元": 0.15, "套息": 0.20, "套利交易": 0.18,
    "美债收益率": 0.18, "国债收益率": 0.15, "收益率曲线": 0.15, "利差": 0.12, "期限利差": 0.12,
    "逆回购": 0.15, "MLF": 0.15, "买断式逆回购": 0.18, "SLF": 0.15, "公开市场": 0.12,
    "M2": 0.15, "信贷": 0.12,
    "离岸人民币": 0.15, "美元指数": 0.15, "人民币汇率": 0.15,
    "VIX": 0.18, "波动率": 0.12, "基差": 0.12, "贴水": 0.12, "融资余额": 0.12, "两融": 0.12, "融资融券": 0.12,
}

POLICY_KEYWORDS = {
    "降准": 0.20, "降息": 0.20, "加息": 0.15,
    "印花税": 0.22, "注册制": 0.15,
    "产业政策": 0.12, "补贴": 0.10, "减税": 0.12,
    "国务院": 0.12, "证监会": 0.10, "央行": 0.10,
    "国家发改委": 0.10, "政策利好": 0.12,
}

SECTOR_KEYWORDS = [
    "半导体", "芯片", "新能源", "光伏", "储能",
    "人工智能", "AI", "大模型", "算力", "数据要素",
    "医药", "创新药", "白酒", "银行", "房地产",
    "军工", "稀土", "煤炭", "钢铁", "有色",
    "消费电子", "汽车", "锂电", "氢能", "机器人",
]

TECH_HARDWARE_KEYWORDS = [
    "CPO", "光模块", "光连接", "光通信", "硅光", "光电", "光芯片", "光互联",
    "PCB", "覆铜板", "线路板", "HDI", "柔性电路板", "FPC",
    "半导体", "芯片", "封测", "晶圆", "光刻", "EDA", "存储芯片", "集成电路", "闪存", "显存",
    "算力", "服务器", "交换机", "液冷", "散热", "GPU", "CPU",
    "HBM", "DDR5", "先进封装", "CoWoS", "混合键合", "TSV", "硅通孔",
    "英伟达", "AMD", "台积电", "海力士", "三星", "ASML", "中芯国际",
]

# 纯英文缩写类科技词：必须用词边界正则匹配，禁止子串匹配。
# 实证：罗氏制药公告正文含医学缩写 "nAMD"(年龄相关性黄斑变性)，子串命中 "AMD"，
# 医药新闻被误判为科技资讯获 ×1.20 加成，压过 sc 更高的半导体资讯（7 对倒挂的根因）。
_TECH_ENGLISH_WORDS = {
    "CPO", "PCB", "HDI", "FPC", "EDA", "GPU", "CPU",
    "HBM", "DDR5", "CoWoS", "TSV", "ASML", "AMD",
}
# 高歧义缩写的医药/化学语境排除词：文本同时命中缩写与排除词时，不视为科技词。
# 词边界正则只能防英文/数字相邻（nAMD/hAMD），防不住中文相邻（如"AMD患者"）。
_TECH_ABBREV_EXCLUDE = {
    "AMD": ["黄斑变性", "眼底", "眼科", "视网膜", "视力"],
}
# 英文缩写词边界正则：前后不能紧跟字母/数字，避免 nAMD/hAMD、STorage 等误命中
_tech_english_pattern = re.compile(
    "|".join(
        rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])" for kw in _TECH_ENGLISH_WORDS
    )
)


def _has_tech_keyword(text: str) -> bool:
    """科技硬件词匹配：中文词子串匹配 + 英文缩写词边界匹配

    修复前用 `any(kw in text for kw in TECH_HARDWARE_KEYWORDS)` 全量子串匹配，
    英文缩写（AMD/CPU 等）在中文文本中极易撞医学/化学缩写（如 nAMD）。
    """
    if not text:
        return False
    # finditer 精确返回实际命中的缩写（避免全局 search 命中 A 却按 B 查排除词）
    for m in _tech_english_pattern.finditer(text):
        kw = m.group(0)
        exclude_words = _TECH_ABBREV_EXCLUDE.get(kw)
        if exclude_words and any(w in text for w in exclude_words):
            continue
        return True
    # 中文科技词（含 "三星" 等中文词，与英文缩写分开处理，保持子串匹配）
    for kw in TECH_HARDWARE_KEYWORDS:
        if kw not in _TECH_ENGLISH_WORDS and kw in text:
            return True
    return False


def _tech_hit_count(text: str) -> int:
    """科技硬件词命中计数（词边界感知，用于 tech_bonus 计算）"""
    if not text:
        return 0
    matched = set()
    for m in _tech_english_pattern.finditer(text):
        matched.add(m.group(0))
    count = 0
    for kw in matched:
        exclude_words = _TECH_ABBREV_EXCLUDE.get(kw)
        if exclude_words and any(w in text for w in exclude_words):
            continue
        count += 1
    for kw in TECH_HARDWARE_KEYWORDS:
        if kw not in _TECH_ENGLISH_WORDS and kw in text:
            count += 1
    return count

MEDIUM_KEYWORDS = [
    "北向资金", "外资", "机构调研", "回购", "增持", "减持",
    "分红", "股权激励", "IPO", "定增", "可转债",
    "研报", "评级", "目标价", "买入", "卖出",
]

NOISE_KEYWORDS = [
    "庆典", "年会", "获奖", "表彰", "周年", "联谊", "晚会",
    "八卦", "娱乐", "明星", "综艺", "电影", "电视剧",
    "广告", "软文", "赞助", "冠名",
]

ANNOUNCEMENT_TIERS = [
    (["立案调查", "重大违法", "破产重整", "破产清算",
      "强制解散", "被接管", "移送司法"], 0.95),
    (["业绩预告", "业绩预增", "业绩预减", "业绩修正", "业绩扭亏", "业绩盈转亏",
      "重大资产重组", "借壳上市", "重大收购", "重大出售",
      "债务违约", "债务展期", "巨额亏损",
      "监管处罚", "行政处罚", "警示函", "监管措施",
      "股票停牌", "复牌"], 0.82),
    (["并购", "收购", "重组", "定增", "非公开发行", "可转债",
      "回购", "增持", "减持", "股东减持", "董监高减持",
      "股权激励", "限制性股票", "员工持股计划",
      "分红", "派息", "送转", "高送转",
      "担保", "对外担保", "委托理财",
      "诉讼", "仲裁", "知识产权"], 0.60),
    (["退市", "终止上市", "实施退市风险", "ST", "*ST",
      "股东大会", "召开股东大会", "通知召开", "增加临时提案",
      "变更会计政策", "变更会计师事务所", "变更保荐人",
      "募集资金", "募集资金使用", "募集资金存放",
      "章程修订", "制度修订", "治理结构",
      "投资者保护", "投资者回报"], 0.35),
    (["投资者关系活动", "调研接待", "投资者交流",
      "日常公告", "更正公告", "补充公告", "勘误",
      "报送材料", "报备材料", "备案",
      "联系方式变更", "办公地址变更",
      "董事会决议", "监事会决议", "独董意见",
      "年报", "半年报", "季报", "一季报", "三季报",
      "定期报告", "报告全文"], 0.12),
]


def _calculate_announcement_importance(text: str) -> float:
    """公告专用重要度: 撤销退市(利好)优先识别, ST/退市(利空)降级, 再按分级表匹配"""
    if any(kw in text for kw in ["撤销退市", "撤销*ST", "撤销ST", "申请撤销", "摘帽",
                                  "撤销退市风险警示", "撤销退市风险"]):
        return 0.75
    if any(kw in text for kw in ["*ST", "ST", "退市", "终止上市", "实施退市风险"]):
        return 0.35
    for keywords, score in ANNOUNCEMENT_TIERS:
        if any(kw in text for kw in keywords):
            return score
    return 0.30


# ============================================================
# 预筛权重表（借鉴 daily_stock_analysis _score_news_relevance）
# ============================================================

_OFFICIAL_HOSTS = {
    "cninfo.com.cn", "sse.com", "sse.com.cn", "szse.cn",
    "hkexnews.hk", "sec.gov", "nasdaq.com", "nyse.com",
    "bse.cn",
}
_OFFICIAL_LABELS = {
    "巨潮资讯", "上交所", "深交所", "港交所", "北交所",
    "上海证券交易所", "深圳证券交易所", "北京证券交易所", "香港交易所",
}


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


_COMPANY_EVENT_TERMS = [
    "业绩预增", "业绩预减", "业绩扭亏", "业绩暴雷", "业绩预告",
    "并购", "重组", "借壳", "收购", "减持", "增持", "回购",
    "立案调查", "退市", "ST", "*ST", "涨停", "跌停",
    "重大合同", "中标", "签约", "债务违约", "监管处罚",
]

_SECTOR_NEWS_TERMS = [
    "行业", "板块", "产业链", "龙头", "概念股", "赛道",
    "sector", "industry", "peers", "supply chain",
]

_MACRO_NEWS_TERMS = [
    # 国内宏观
    "大盘", "指数", "宏观", "央行", "利率", "通胀", "降准", "降息",
    "A股", "印花税", "注册制", "北向资金", "外资",
    # 外围股市
    "美股", "纳指", "标普", "道指", "纳斯达克",
    "港股", "恒生", "恒生科技", "南向资金",
    "日经", "日股", "韩指", "韩国", "KOSPI",
    "欧股", "德国DAX", "法国CAC", "英国富时",
    # 美联储/货币政策
    "美联储", "fed", "鲍威尔", "非农", "CPI", "PPI",
    "加息", "缩表", "QE", "点阵图",
    "欧央行", "日央行", "日本央行", "BOJ",
    "inflation",
    # 地缘政治
    "战争", "冲突", "制裁", "地缘", "俄乌", "中东", "巴以",
    "台海", "朝鲜", "核武", "导弹", "无人机袭击",
    "军事", "政变", "恐怖袭击",
    # 大宗商品
    "原油", "WTI", "布伦特", "油价", "黄金", "白银",
    "铜价", "铁矿石", "大宗商品", "有色金属",
    # 金融风险
    "暴跌", "熔断", "崩盘", "债务危机", "银行危机",
    "汇率", "美元", "美债", "日元", "人民币汇率",
    "金融风险", "系统性风险", "流动性危机",
    # 国际贸易
    "关税", "贸易战", "出口管制", "禁运",
]


def score_news_relevance(item: dict, stock_code: str = "", stock_name: str = "") -> tuple:
    """资讯关联度打分（借鉴 DSA _score_news_relevance，适配资讯流场景）

    权重表：
      代码命中 标题+55 / 摘要+34 / URL+18
      公司名命中 标题+45 / 摘要+28
      事件词+12（资讯流场景独立触发）
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

    if stock_code:
        if stock_code in title:
            score += 55; direct_signal += 55
        elif stock_code in content:
            score += 34; direct_signal += 34
        elif stock_code in url:
            score += 18; direct_signal += 18

    if stock_name:
        if stock_name in title:
            score += 45; direct_signal += 45
        elif stock_name in content:
            score += 28; direct_signal += 28

    # 事件词加分：有 direct_signal 时按设计 +12；无 direct_signal（纯资讯流）时降为 +6
    # 避免高频词（涨停/跌停）过度加分，但保留有价值的事件信号
    if any(t in text for t in _COMPANY_EVENT_TERMS):
        if direct_signal > 0:
            score += 12; direct_signal += 12
        else:
            score += 6

    if _has_tech_keyword(text):
        score += 10

    if _is_official_source(url, item.get("source", "")):
        score += 8

    if any(t in text for t in _SECTOR_NEWS_TERMS):
        score += 6

    is_macro = any(t in text for t in _MACRO_NEWS_TERMS)
    # 政策性词汇（央行/降准/降息等）不罚分，只有纯宏观评论才罚分
    has_policy = any(kw in text for kw in POLICY_KEYWORDS)

    # 外围资讯精细化分层：只对"影响全球或A股科技板块"的外围资讯加分
    # 核心外围关键词：直接传导A股科技/全球系统性风险
    _GLOBAL_TECH_RISK_TERMS = [
        # 科技管制（直接传导A股科技板块）
        "制裁", "出口管制", "禁运", "技术封锁", "实体清单", "关税", "贸易战",
        # 全球系统性风险（影响全球市场）
        "美联储", "加息", "降息", "缩表", "QE", "鲍威尔", "非农",
        "熔断", "崩盘", "债务危机", "银行危机", "金融风险", "系统性风险",
        "原油暴涨", "原油暴跌",
        # 地缘冲突（影响全球风险偏好）
        "战争", "军事冲突", "冲突", "地缘",
        # 外围股市剧变（传导A股情绪）
        "暴跌",
    ]
    global_tech_hits = sum(1 for kw in _GLOBAL_TECH_RISK_TERMS if kw in text)

    # 纯外围资讯罚分条件：是 macro 类 + 无 direct_signal + 无政策词 + 无核心外围关键词
    if is_macro and direct_signal == 0 and not has_policy and global_tech_hits == 0:
        score -= 12

    # 核心外围事件加分：只对影响全球/A股科技板块的外围资讯加分
    if global_tech_hits > 0:
        score += min(global_tech_hits * 5, 15)

    score = max(0, min(100, score))

    if direct_signal >= 38:
        category = "direct"
    elif is_macro and direct_signal == 0:
        category = "macro"
    else:
        category = "sector"
    return score, category


def calculate_prefilter_importance(news: dict) -> float:
    """计算新闻预过滤重要度 — 多因子叠加模型
    
    公告类: 使用精细分级表并以 Sigmoid 压缩 (退市0.95 ~ 投资者关系0.12)
    新闻类: 多因子叠加并以 Sigmoid 压缩 (高影响+政策+板块+中等信号+LLM加成)
    
    范围: 0.05 ~ 0.99
    """
    title = news.get("title", "")
    content = news.get("content", "")
    name = news.get("name", "")
    category = news.get("category", "news")

    clean_title = title.replace(name, "") if name else title
    clean_content = content.replace(name, "") if name else content
    text = f"{clean_title} {clean_content}"

    if any(kw in text for kw in NOISE_KEYWORDS):
        return 0.10

    # 信号情报(龙虎榜/业绩预告): 已预标注方向, 按信号强度直接评分
    if category == "signal":
        direction = news.get("impact_direction", "neutral")
        base = 0.55 if direction in ("bullish", "bearish") else 0.35

        import re
        num_match = re.search(r'幅度([+-]?[\d.]+)%', text)
        amplitude_bonus = 0.0
        if num_match:
            amplitude = abs(float(num_match.group(1)))
            if amplitude > 1000:
                amplitude_bonus = 0.25
            elif amplitude > 500:
                amplitude_bonus = 0.20
            elif amplitude > 100:
                amplitude_bonus = 0.15
            elif amplitude > 50:
                amplitude_bonus = 0.10

        lhb_bonus = 0.10 if "机构净买入" in text or "龙虎榜" in text else 0.0
        stock_bonus = 0.05 if news.get("affected_stocks") else 0.0

        raw_sum = base + amplitude_bonus + lhb_bonus + stock_bonus
        importance = 0.99 * math.tanh(1.4 * raw_sum)
        has_tech = _has_tech_keyword(text)
        if not has_tech:
            source = news.get("source", "")
            exempt = any(es in source for es in ["龙虎榜", "业绩预告"])
            if not exempt:
                importance *= 0.65
        return round(importance, 2)

    # 公告类
    if category == "announcement":
        base = _calculate_announcement_importance(text)
        direction = news.get("impact_direction", "neutral")
        
        if direction == "neutral" and base >= 0.75:
            base *= 0.6

        tech_hits = _tech_hit_count(text)
        tech_bonus = min(tech_hits * 0.15, 0.40)

        llm_bonus = 0.0
        if direction in ("bullish", "bearish"):
            llm_bonus += 0.06
        if news.get("affected_sectors"):
            llm_bonus += min(len(news["affected_sectors"]) * 0.02, 0.06)
        if news.get("affected_stocks"):
            llm_bonus += min(len(news["affected_stocks"]) * 0.03, 0.06)

        raw_sum = base + tech_bonus + llm_bonus
        importance = 0.99 * math.tanh(1.4 * raw_sum)
        has_tech = _has_tech_keyword(text)
        if not has_tech:
            source = news.get("source", "")
            exempt_sources = ["央视新闻", "新华社", "人民日报", "经济日报",
                              "证监会", "央行", "国务院", "上海证券交易所",
                              "深圳证券交易所", "北京证券交易所", "北交所",
                              "中国证券报", "上海证券报", "证券时报", "证券日报"]
            is_exempt = any(es in source for es in exempt_sources)
            if not is_exempt:
                importance *= 0.65
        return round(importance, 2)

    # 新闻类: 多因子叠加
    score = 0.10
    direction = news.get("impact_direction", "neutral")

    high_bonus = sum(s for kw, s in HIGH_IMPACT_KEYWORDS.items() if kw in text)
    if direction == "neutral" and high_bonus > 0:
        high_bonus *= 0.3
    high_cat = min(high_bonus, 0.45)

    policy_bonus = sum(s for kw, s in POLICY_KEYWORDS.items() if kw in text)
    policy_cat = min(policy_bonus, 0.35)

    tech_hits = _tech_hit_count(text)
    tech_cat = min(tech_hits * 0.20, 0.50)
    other_sector_hits = sum(1 for kw in SECTOR_KEYWORDS if kw in text and kw not in TECH_HARDWARE_KEYWORDS)
    sector_cat = min(other_sector_hits * 0.04, 0.10)

    medium_hits = sum(1 for kw in MEDIUM_KEYWORDS if kw in text)
    medium_cat = min(medium_hits * 0.04, 0.12)

    llm_bonus = 0.0
    if direction in ("bullish", "bearish"):
        llm_bonus += 0.08
    if news.get("affected_sectors"):
        llm_bonus += min(len(news["affected_sectors"]) * 0.02, 0.06)
    if news.get("affected_stocks"):
        llm_bonus += min(len(news["affected_stocks"]) * 0.03, 0.06)

    raw_sum = score + high_cat + policy_cat + tech_cat + sector_cat + medium_cat + llm_bonus
    importance = 0.99 * math.tanh(1.4 * raw_sum)
    is_stock_related = bool(news.get("affected_stocks"))
    has_tech = _has_tech_keyword(text)
    if is_stock_related and not has_tech:
        source = news.get("source", "")
        exempt_sources = ["央视新闻", "新华社", "人民日报", "经济日报",
                          "证监会", "央行", "国务院", "上海证券交易所",
                          "深圳证券交易所", "北京证券交易所", "北交所",
                          "中国证券报", "上海证券报", "证券时报", "证券日报"]
        is_exempt = any(es in source for es in exempt_sources)
        if not is_exempt:
            importance *= 0.65
    return round(importance, 2)


# ============================================================
# 时间因子 (仅展示用, 不参与排序)
# ============================================================

def calculate_time_factor(published_at: str) -> float:
    """计算时间新鲜度 (参与主评分: total × (0.90 + 0.10×tf), 也用于排序第三键)

    使用北京时间(BJT)比较，避免云端 GitHub Actions 运行在 UTC 时区时
    因 naive datetime.now() 与北京时间新闻串产生 8 小时偏差，
    导致当日资讯 hours_diff 恒为负 → 钳位 0 → time_factor 全部顶格 1.00。
    """
    if not published_at or not published_at.strip():
        return 0.0

    BJT = timezone(timedelta(hours=8))
    now = datetime.now(BJT)
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d", "%Y%m%d", "%Y/%m/%d %H:%M:%S"]:
        try:
            pub_time = datetime.strptime(published_at.strip(), fmt)
            # strptime 返回 naive datetime，加 BJT 时区才能与 aware now 正确比较
            pub_time = pub_time.replace(tzinfo=BJT)
            hours_diff = (now - pub_time).total_seconds() / 3600
            if hours_diff < 0:
                hours_diff = 0
            if hours_diff <= 1:
                return 1.00
            elif hours_diff <= 3:
                return 0.95
            elif hours_diff <= 6:
                return 0.90
            elif hours_diff <= 12:
                return 0.85
            elif hours_diff <= 24:
                return 0.80
            else:
                return 0.60
        except ValueError:
            continue
    return 0.0


# ============================================================
# 综合排名 — 改进版排序规则
# ============================================================

NEWS_CRED_WEIGHT = 0.15
NEWS_IMP_WEIGHT = 0.70
NEWS_CLUSTER_WEIGHT = 0.15

ANN_CRED_WEIGHT = 0.05
ANN_IMP_WEIGHT = 0.95

SIGNAL_CRED_WEIGHT = 0.10
SIGNAL_IMP_WEIGHT = 0.90

# 国家级权威来源标识 (优先级高于科技加成)
NATIONAL_AUTHORITY_SOURCES = [
    "央视新闻", "新华社", "人民日报", "经济日报", "光明日报",
    "证监会", "央行", "国务院", "银保监会", "国家发改委",
    "上海证券交易所", "深圳证券交易所", "北京证券交易所", "北交所",
    "中国证券报", "上海证券报", "证券时报", "证券日报",
]

# 国家级政策关键词 (触发国家优先级)
NATIONAL_POLICY_KEYWORDS = [
    "降准", "降息", "加息", "印花税", "注册制",
    "国务院", "央行", "证监会", "国家发改委",
    "货币政策", "财政政策", "宏观调控", "国家级",
    "人民银行", "外汇储备", "逆回购", "MLF", "LPR",
]


# ============================================================
# 规则方向判定词表（2026-08-03 结构化 + 扩充）
# 使用约定：标题优先命中即定方向；正文仅在标题无信号时扫描首句。
# 注意：与 HIGH_SIGNAL_KEYWORDS（路由/预筛语义）不同，本词表仅用于方向兜底，
#       "回购/增持"裸词不纳入利多（避免中小市值个股自身消息被规则直通）。
# ============================================================
STRONG_BULLISH_KEYWORDS = [
    # 摘帽/扭亏
    "撤销退市", "撤销*ST", "撤销ST", "撤销风险警示", "申请撤销", "摘帽",
    "扭亏为盈", "业绩扭亏", "扭亏",
    # 业绩
    "业绩预增", "业绩超预期", "大幅预增", "大幅增长", "业绩大增",
    # 回购/增持
    "股份回购", "大额回购", "回购股份", "股东增持", "董监高增持", "管理层增持",
    # 订单
    "中标", "签约", "大额订单", "重大合同",
    # 政策
    "降准", "降息", "减税降费", "减税", "政策利好", "产业扶持", "财政补贴",
    # 资本运作
    "并购重组", "重大资产重组", "借壳上市",
    # 价格上涨类（LLM截断兜底 + 2026-08-03 扩充：涨超/普涨/走高/上行/回升/涨价/上调目标价）
    "大涨", "暴涨", "飙升", "涨停", "涨幅显著", "涨幅扩大", "涨超", "普涨",
    "价格大涨", "价格暴涨", "价格飙升", "走高", "上行", "回升", "涨价",
    "上调目标价", "上调", "逆势上涨", "涨逾",
]

STRONG_BEARISH_KEYWORDS = [
    # 监管/违法
    "立案调查", "重大违法", "破产重整", "破产清算", "被接管",
    "监管处罚", "行政处罚", "警示函", "监管措施", "监管问责",
    # 业绩暴雷
    "业绩暴雷", "巨额亏损", "债务违约", "重大违约", "业绩预减", "业绩盈转亏",
    "首亏", "亏损扩大", "负增长",
    # 退市
    "退市", "终止上市", "实施退市风险", "*ST",
    # 减持
    "股东减持", "董监高减持", "大股东减持", "减持",
    # 价格下跌类（2026-08-03 扩充：跌超/走弱/走低/低开低走/回调/下滑/下跌/重创/监管问责/熔断）
    "跌停", "大跌", "暴跌", "跳水", "重挫", "重创", "跌超", "走弱", "走低",
    "低开低走", "回调", "下滑", "下跌", "跌逾", "熔断", "逆势下跌", "下调",
    "价格大跌", "价格暴跌", "价格跳水", "产品降价", "下调目标价",
    # 其他利空
    "跑路", "爆雷", "行业利空",
    # 外部制裁
    "出口管制", "制裁", "禁运", "断供", "贸易摩擦",
]

# 反向修正前缀：前缀后 12 字内出现利空词 → 判利好（放宽出口管制）；
# 出现利好词 → 判利空（取消增持/终止回购）。
_REVERSE_PREFIXES = ["放宽", "放松", "解除", "取消", "移除", "结束", "终止", "停止"]


def predict_direction_by_rules(title: str, content: str) -> str:
    """通过规则快速兜底判定多空方向（2026-08-03 重写：标题优先 + 事件句对齐）

    修复记录（用户反馈"跳过LLM的资讯利空显示利好"）:
    1. 标题优先：方向只由标题强关键词决定——标题是事件本身。
    2. 正文仅在标题无信号时扫描"首句"（主表述），不再全文扫描——
       避免正文中其他板块/个股的上涨词（"仅XX逆势大涨""XX涨停"）
       把利空事件整体翻成利好。
    3. 反向修正：放宽/放松/解除/取消/移除/结束/终止/停止 等前缀 + 12字内
       利空词 → 判利好（如"马来西亚考虑放宽稀土出口管制"）；前缀 + 利好词 → 判利空。
    4. 词表扩充：补 跌超/走弱/走低/低开低走/回调/下滑/下跌/重创/监管问责/熔断
       等常见利空词；涨超/普涨/走高/上行/回升/涨价 等利多词。
    """
    title_text = str(title or "")
    content_text = str(content or "")

    # 否定词表（扩展）：覆盖单字否定、双字否定及隐含否定动词
    # 奇数个否定 = 取反；偶数个 = 双重否定表肯定（如"未能否认"=承认）
    _NEGATION_WORDS = [
        "不", "未", "没", "无", "非", "勿", "莫",
        "没有", "未能", "不能", "不会", "不再", "无从",
        "取消", "终止", "撤销", "撤回", "中止", "停止", "废除", "解除",
        "失败", "拒绝", "否认", "否定", "驳回", "推翻",
    ]

    def _count_negations_before(text: str, kw: str, window: int = 8) -> int:
        """统计关键词前 window 字符窗口内的否定词数量

        双重否定处理：奇数个否定词 → 取反；偶数个 → 互相抵消（表肯定）。
        窗口式扫描替代原先的"紧邻完全匹配"，能捕获"未能否认业绩预增"
        这类中间隔字词的语境。

        非重叠计数：按长度降序贪心匹配，长否定词优先消耗字符位，
        避免"未"与"未能"、"不"与"不再"等包含关系导致同一否定被重复计数。

        Args:
            text: 待扫描文本（标题 或 正文首句）
            kw: 目标关键词
            window: 关键词向前扫描的字符窗口大小

        Returns:
            窗口内否定词总数（0 表示无否定）
        """
        count = 0
        idx = text.find(kw)
        # 按长度降序：长否定词优先匹配，防止单字否定与双字否定重叠计数
        sorted_neg = sorted(_NEGATION_WORDS, key=len, reverse=True)
        while idx >= 0:
            start = max(0, idx - window)
            prefix_text = text[start:idx]
            consumed = [False] * len(prefix_text)
            for i in range(len(prefix_text)):
                if consumed[i]:
                    continue
                for neg in sorted_neg:
                    end = i + len(neg)
                    if end <= len(prefix_text) and prefix_text[i:end] == neg:
                        for j in range(i, end):
                            consumed[j] = True
                        count += 1
                        break
            idx = text.find(kw, idx + 1)
        return count

    def _has_negation(text: str, kw: str) -> bool:
        """关键词前窗口内否定词数量为奇数则视为否定（双重否定抵消）"""
        return _count_negations_before(text, kw) % 2 == 1

    def _scan(text: str) -> str:
        """单段文本方向：强利多词 → 强利空词（均需未否定）"""
        if not text:
            return "neutral"
        for kw in STRONG_BULLISH_KEYWORDS:
            if kw in text and not _has_negation(text, kw):
                return "bullish"
        for kw in STRONG_BEARISH_KEYWORDS:
            if kw in text and not _has_negation(text, kw):
                return "bearish"
        return "neutral"

    # ① 反向修正（双向）：放宽/解除/取消 + 12字内利空词 → 利好；+ 利好词 → 利空
    # 反向检测额外纳入裸词 增持/回购/并购/重组/中标/签约（"取消增持""终止回购"是利空）
    full = f"{title_text} {content_text}"
    _REV_BULLISH = STRONG_BULLISH_KEYWORDS + ["增持", "回购", "并购", "重组", "中标", "签约"]
    for prefix in _REVERSE_PREFIXES:
        idx = full.find(prefix)
        if idx < 0:
            continue
        window = full[idx: idx + len(prefix) + 12]
        for kw in STRONG_BEARISH_KEYWORDS:
            if kw in window:
                return "bullish"
        for kw in _REV_BULLISH:
            if kw in window:
                return "bearish"

    # ② 标题优先（主信号）
    d = _scan(title_text)
    if d != "neutral":
        return d

    # ③ 正文首句兜底（主表述；不扫描全文，避免"仅XX逆势大涨"等非事件句污染方向）
    first_sentence = re.split(r"[。；;！!？?]", content_text, maxsplit=1)[0]
    return _scan(first_sentence)


SECTOR_NAME_MAP = {
    "CPO": "CPO/光通信", "光模块": "CPO/光通信", "光连接": "CPO/光通信",
    "光通信": "CPO/光通信", "硅光": "CPO/光通信", "光芯片": "CPO/光通信", "光互联": "CPO/光通信",
    "PCB": "PCB/电路板", "覆铜板": "PCB/电路板", "线路板": "PCB/电路板",
    "HDI": "PCB/电路板", "柔性电路板": "PCB/电路板", "FPC": "PCB/电路板",
    "半导体": "半导体", "芯片": "半导体", "封测": "半导体", "晶圆": "半导体",
    "光刻": "半导体", "EDA": "半导体", "集成电路": "半导体", "存储芯片": "半导体",
    "闪存": "半导体", "显存": "半导体", "代工": "半导体",
    "算力": "算力/服务器", "服务器": "算力/服务器", "交换机": "算力/服务器",
    "液冷": "算力/服务器", "散热": "算力/服务器", "GPU": "算力/服务器", "CPU": "算力/服务器",
    "HBM": "存储/HBM", "DDR5": "存储/HBM", "先进封装": "半导体", "CoWoS": "半导体",
    "新能源": "新能源", "光伏": "光伏", "储能": "储能",
    "人工智能": "人工智能", "AI": "人工智能", "大模型": "人工智能",
    "数据要素": "数据要素",
    "医药": "医药", "创新药": "医药",
    "白酒": "白酒", "银行": "银行", "房地产": "房地产", "地产": "房地产",
    "军工": "军工", "稀土": "稀土", "煤炭": "煤炭", "钢铁": "钢铁", "有色": "有色金属",
    "消费电子": "消费电子", "汽车": "汽车", "锂电": "锂电池", "氢能": "氢能", "机器人": "机器人",
    # 宏观政策词不映射到具体板块（下游作为宏观处理）
}


def infer_sectors_by_rules(title: str, content: str, name: str = "") -> list:
    """基于关键词规则推断影响板块 (LLM 未填写时的兜底)"""
    clean_title = title.replace(name, "") if name else title
    clean_content = content.replace(name, "") if name else content
    text = f"{clean_title} {clean_content}"

    sectors = []
    seen = set()
    for kw, sector_name in SECTOR_NAME_MAP.items():
        if kw in text and sector_name not in seen:
            seen.add(sector_name)
            sectors.append(sector_name)
    return sectors[:5]


def _is_national_authority(source: str) -> bool:
    """判断是否国家级权威来源"""
    if not source:
        return False
    return any(na in source for na in NATIONAL_AUTHORITY_SOURCES)


def _has_national_policy(text: str) -> bool:
    """判断是否涉及国家级政策"""
    return any(np in text for np in NATIONAL_POLICY_KEYWORDS)


BAND_PRIORITY = {
    "bullish": 6, "bearish": 6, "mixed": 6,     # 有明确多空信号的强资讯并列高位（重要资讯在前）
    "mildly_bullish": 4, "mildly_bearish": 4,   # 中等信号并列
    "neutral": 1,                               # 无明确多空方向的弱信号排后
}

# 影响范围分数加成（融入 total_score，不再作为独立排序主键）
# 设计意图：market 级事件通常影响面更广，给予适度分数加成使其在同 band 内优先。
# 修复：market 加成 0.12 → 0.20。实证 #1 MSCI AI观点(sector, 0.815) 压过 #2 富达美联储(market, 0.683)，
# 科技×1.20 与非科技×0.85 的倍率差（1.41x）完全盖过 0.12 加成；+0.20 后同影响强度下 market 反超 sector。
# 但加成仍远小于正常分数差异，确保高分 sector 事件能越过低分 market 事件，
# 避免"0.37 的 market 压住 0.82 的 sector"的倒挂。
# stock 级个股资讯轻微降权，让板块/市场级资讯自然前置。
SCOPE_SCORE_BOOST = {
    "market": 0.20,
    "sector": 0.0,
    "stock": -0.05,
}


def _infer_influence_scope(news: dict, hs300: dict = None) -> str:
    """推断资讯的影响范围层级（LLM 未输出 influence_scope 时的后备规则）

    判断逻辑：
    - market: 国家级权威+政策关键词，或跨3+板块
    - sector: 有受影响板块，或个股是沪深300龙头（龙头带动效应）
    - stock: 仅个股，无板块关联
    """
    title = news.get("title", "")
    content = news.get("content", "")
    text = f"{title} {content}"
    source = news.get("source", "")
    affected_sectors = news.get("affected_sectors", []) or []
    affected_stocks = news.get("affected_stocks", []) or []
    stock_name = news.get("name", "")

    # 市场级：国家级权威 + 政策关键词，或跨3+板块
    if (_is_national_authority(source) and _has_national_policy(text)) or len(affected_sectors) >= 3:
        return "market"

    # 板块级：有受影响板块，或个股是沪深300龙头
    if affected_sectors:
        return "sector"
    if hs300 is not None:
        stocks_to_check = list(affected_stocks) + ([stock_name] if stock_name else [])
        for s in stocks_to_check:
            if s and _is_hs300_stock(s, "", hs300):
                return "sector"

    return "stock"

CONFIDENCE_WEIGHT = {
    "high": 1.0, "medium": 0.90, "low": 0.7,
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
    """band 降一档（向 neutral 方向降级，降低冲突项的排序优先级）

    BAND_PRIORITY: bullish/bearish/mixed=6, mildly_*=4, neutral=1
    降级 = 向 neutral 方向移动一档，确保冲突项的 band_priority 必然降低。
    旧实现用线性数组 idx+1，导致 bearish 不降级、mildly_* 反而升级——已修正。
    """
    _DOWNGRADE = {
        "bullish": "mildly_bullish",       # 6 → 4
        "mildly_bullish": "neutral",       # 4 → 1
        "mixed": "neutral",                # 6 → 1
        "neutral": "neutral",              # 1 → 1（已最低，不变）
        "mildly_bearish": "neutral",       # 4 → 1
        "bearish": "mildly_bearish",       # 6 → 4
    }
    return _DOWNGRADE.get(band, "neutral")


def _load_watchlist():
    """加载自定义关注股票/板块列表（来自 watchlist.json，按文件 mtime 缓存失效）

    原实现为进程级永久缓存,修改 watchlist.json 后需重启服务才生效。
    现记录文件修改时间,mtime 变化时自动重新加载。
    """
    global _watchlist_cache
    try:
        # calculators.py 在 src/tools/，需三级 parent 才到项目根
        watchlist_path = Path(__file__).parent.parent.parent / "watchlist.json"
        mtime = watchlist_path.stat().st_mtime if watchlist_path.exists() else 0
        # mtime 未变且已缓存 → 直接返回
        if (_watchlist_cache is not None
                and _watchlist_cache.get("_mtime") == mtime):
            return _watchlist_cache
        if watchlist_path.exists():
            with open(watchlist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _watchlist_cache = {
                "stocks": set(data.get("stocks", [])),
                "sectors": set(data.get("sectors", [])),
                "_mtime": mtime,
            }
            if _watchlist_cache["stocks"] or _watchlist_cache["sectors"]:
                logger.info(f"关注列表已加载: {len(_watchlist_cache['stocks'])}只个股, {len(_watchlist_cache['sectors'])}个板块")
        else:
            _watchlist_cache = {"stocks": set(), "sectors": set(), "_mtime": 0}
    except Exception as e:
        logger.warning(f"加载 watchlist.json 失败: {e}")
        if _watchlist_cache is None:
            _watchlist_cache = {"stocks": set(), "sectors": set(), "_mtime": 0}
    return _watchlist_cache


def _is_hs300_stock(stock_name: str, stock_code: str, hs300: dict) -> bool:
    """判断个股是否在沪深300成分股中。

    hs300 为空（获取失败）时保守返回 True（不降权），避免误伤。
    优先代码精确匹配，其次名称精确匹配。
    """
    if not hs300:
        return True
    codes = hs300.get("codes", set()) or set()
    names = hs300.get("names", set()) or set()
    if not codes and not names:
        return True
    stock_name = (stock_name or "").strip()
    stock_code = (stock_code or "").strip().zfill(6)
    if stock_code and stock_code in codes:
        return True
    if stock_name and stock_name in names:
        return True
    return False


# 高影响公告关键词：即使非龙头股，命中这些词的公告也保留
_HIGH_IMPACT_ANN_KEYWORDS = [
    "立案调查", "重大违法", "破产重整", "破产清算", "强制解散", "被接管",
    "业绩预告", "业绩预增", "业绩预减", "业绩修正", "业绩扭亏", "业绩盈转亏",
    "重大资产重组", "借壳上市", "重大收购", "重大出售",
    "债务违约", "债务展期", "巨额亏损",
    "监管处罚", "行政处罚", "警示函", "监管措施",
    "股票停牌", "复牌", "退市", "终止上市", "实施退市风险",
    "撤销退市", "撤销*ST", "撤销ST", "摘帽",
]


def is_high_impact_announcement(news: dict) -> bool:
    """判断公告是否为高影响公告（无论是否龙头股都应保留）

    判断标准：命中高影响关键词（立案/退市/重组/业绩预告/债务违约等）
    """
    title = news.get("title", "")
    content = news.get("content", "")
    text = f"{title} {content}"
    return any(kw in text for kw in _HIGH_IMPACT_ANN_KEYWORDS)


# 科技龙头股名单（沪深300可能未覆盖的科创板/创业板科技龙头）
# 这些个股的公告即使非高影响也保留，因为其动向对科技板块有风向标意义
_TECH_LEADER_STOCKS = {
    # 代码集合
    "688256",  # 寒武纪
    "688041",  # 海光信息
    "300308",  # 中际旭创
    "300502",  # 新易盛
    "002371",  # 北方华创
    "603501",  # 韦尔股份
    "300661",  # 圣邦股份
    "688008",  # 澜起科技
    "002463",  # 沪电股份
    "300394",  # 天孚通信
    "002281",  # 光迅科技
    "688599",  # 天合储能
    "688036",  # 传音控股
    "688185",  # 康希诺
    "300274",  # 阳光电源
    "688590",  # 新致软件
    "300750",  # 宁德时代
    "688981",  # 中芯国际
    "601138",  # 工业富联
    "600584",  # 长电科技
}
_TECH_LEADER_NAMES = {
    "寒武纪", "海光信息", "中际旭创", "新易盛", "北方华创",
    "韦尔股份", "圣邦股份", "澜起科技", "沪电股份", "天孚通信",
    "光迅科技", "传音控股", "阳光电源", "宁德时代", "中芯国际",
    "工业富联", "长电科技", "中兴通讯", "紫光国微", "兆易创新",
}


def is_leader_or_high_impact(news: dict, hs300: dict) -> bool:
    """判断公告是否来自龙头股或是高影响公告

    Returns:
        True = 龙头股公告或高影响公告，应保留
        False = 非龙头股的低影响常规公告，应过滤
    """
    # 高影响公告始终保留
    if is_high_impact_announcement(news):
        return True
    # 龙头股（沪深300）的公告保留
    name = news.get("name", "")
    code = news.get("code", "")
    if _is_hs300_stock(name, code, hs300):
        return True
    # 科技龙头股（沪深300可能未覆盖的科创板/创业板龙头）保留
    code_filled = (code or "").strip().zfill(6)
    if code_filled in _TECH_LEADER_STOCKS:
        return True
    if name and name in _TECH_LEADER_NAMES:
        return True
    return False


def _is_self_only_individual_stock(news: dict, hs300: dict) -> bool:
    """预筛判定：一条资讯是否"仅影响个股自身"（应在预筛剔除）

    用户明确需求：其他个股只能影响自身 → 预筛阶段剔除；最终只保留能带动市场情绪的价值资讯
    （全球宏观 > 科技板块 > 龙头个股）。

    判定流程：
      1. 公告类不在此处理（由 is_leader_or_high_impact 决定保留/剔除）
      2. 龙头股命中（沪深300 名称/代码 或 科技龙头名称/代码）→ 带动板块情绪，保留（非 self-only）
      3. 是否提及某个具体个股？
         - 有 name / code 字段，或标题/正文含 A 股 6 位代码（0/3/6/8/4/9 开头）
         - 未提及具体个股 → 保守保留（不误杀，交由 ranking 的 scope 维度沉底）
      4. 含板块/宏观关键词 → 具备板块或市场联动，保留
      全满足（提及具体非龙头个股 + 无板块/宏观联动）→ 仅影响自身，剔除。

    设计取舍：不做"高影响事件"豁免。非龙头股的"中标/业绩预增"等即便数值亮眼，
    也只影响该股自身、不带动板块情绪，按用户需求剔除，避免噪声稀释头部价值资讯。
    """
    if news.get("category") == "announcement":
        return False

    title = news.get("title", "")
    content = news.get("content", "")
    name = news.get("name", "")
    text = f"{title} {content}"
    clean_text = text.replace(name, "") if name else text

    # 龙头股命中（沪深300 名称/代码 + 科技龙头名称/代码）→ 带动板块情绪，保留
    leader_names = set(_TECH_LEADER_NAMES)
    leader_codes = set(_TECH_LEADER_STOCKS)
    if hs300 and hs300.get("names"):
        leader_names |= set(hs300["names"])
    if hs300 and hs300.get("codes"):
        leader_codes |= set(hs300["codes"])
    # 先直接检查本条目自身的 name/code（clean_text 已剥离 name，否则本身是龙头时无法识别）
    if name and name in leader_names:
        return False
    code = news.get("code", "")
    if code and code in leader_codes:
        return False
    # 再扫描文本中是否提及其他龙头股（clean_text 已去除条目自身 name，避免重复触发）
    for ln in leader_names:
        if ln and ln in clean_text:
            return False

    # 是否提及某个具体个股（无板块/市场联动信号的个股新闻才可能是 self-only）
    mentions_specific_stock = bool(name) or bool(code)
    if not mentions_specific_stock:
        # A 股 6 位代码（0/3/6/8/4/9 开头，前后非数字）出现 → 具体个股
        mentions_specific_stock = bool(re.search(r'(?<!\d)[0-9]{6}(?!\d)', clean_text))
    if not mentions_specific_stock:
        # 未提及具体个股 → 保守保留，由 ranking 的 scope 维度处理
        return False

    # 板块/宏观关键词 → 具备板块或市场联动，保留
    if any(kw in clean_text for kw in SECTOR_KEYWORDS + _MACRO_NEWS_TERMS):
        return False

    # 否则：提及某非龙头个股，且无板块/市场联动 → 仅影响自身，剔除
    return True


def _calc_continuous_score(news: dict, hs300: dict = None) -> float:
    """连续分数计算 — 保留原 rank_news 循环体内全部计算逻辑

    total = (w_cred × 可信度 + w_imp × LLM重要度 [+ w_cluster × 聚类热度]) × 方向折扣 × 科技加成
    - 方向折扣: 非中性=1.0; 中性按 LLM 重要度分级 (高分不折/中分轻折/低分重折)
    - 科技加成: 科技资讯统一×1.20; 国家级政策×1.15; 非科技非国家级×0.85; 市场级豁免降权
    - 国家级权威来源 + 政策关键词 → 额外加成
    - ST/退市类垃圾股进一步降级
    - 不封顶：scope 作为排序第一键后，同 scope 内需让 LLM 价值评分充分表达差异
    """
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

    # ---- 改进的加成/降级逻辑 ----
    title = news.get("title", "")
    content = news.get("content", "")
    name = news.get("name", "")
    clean_title = title.replace(name, "") if name else title
    clean_content = content.replace(name, "") if name else content
    clean_text = f"{clean_title} {clean_content}"
    # ST/退市检测使用原始文本（title+content），避免公司名称含"*ST"前缀被误删
    raw_text = f"{title} {content}"

    is_tech = _has_tech_keyword(clean_text)
    is_national_auth = _is_national_authority(news.get("source", ""))
    is_national_policy = _has_national_policy(clean_text)
    # ST/退市检测：用正则精确匹配 *ST/ST 前缀（公司名），避免误命中英文缩写如 STorage/STMicroelectronics
    import re as _re
    is_st_delist = bool(
        _re.search(r'\*ST', raw_text)
        or _re.search(r'(?<![A-Za-z])ST(?![A-Za-z])', raw_text)  # ST 前后不能是英文字母
        or "退市" in raw_text
        or "终止上市" in raw_text
    )

    # ---- 影响范围（scope）：不再对分数加权，由 rank_news 排序时统一加成 ----
    # scope 加成在 rank_news 的排序键中统一应用（SCOPE_SCORE_BOOST），
    # 此处仅计算 scope 供后续科技/降权逻辑使用。
    scope = news.get("influence_scope", "")
    if not scope:
        scope = _infer_influence_scope(news, hs300)

    # ---- 科技板块统一加权 / 非科技统一降权 ----
    # CPO/PCB/半导体等科技硬件词命中：不管利好利空统一显著加成
    # 非科技资讯：国家级政策保持加成，其他统一降权
    # 市场级资讯豁免非科技降权（央行降息等不应因无科技关键词被降权）
    if is_tech:
        total = round(total * 1.20, 4)   # 科技资讯 x1.20 (降低，避免过度压制非科技)
    elif is_national_auth and is_national_policy:
        total = round(total * 1.15, 4)   # 国家级政策 x1.15 (提高，央行/财政部等应受重视)
    elif is_national_auth:
        total = round(total * 1.08, 4)   # 国家级来源 x1.08
    elif scope == "market":
        pass  # 市场级豁免非科技降权
    else:
        total = round(total * 0.85, 4)   # 非科技非国家级资讯降权 x0.85 (提高基线)

    # ---- 沪深300成分股过滤 + ST/退市分级降权 ----
    # 仅对个股级资讯生效：sector/market 级资讯的影响范围是整个板块/市场，
    # affected_stocks 可能含海外公司（如OpenAI/微软），不应因此降权
    affected_stocks = news.get("affected_stocks", []) or []
    stock_name = news.get("name", "")
    stock_code = news.get("code", "")
    has_individual_stock = bool(affected_stocks or stock_name or stock_code)

    all_non_hs300 = True
    if has_individual_stock and hs300 is not None:
        stocks_to_check = list(affected_stocks)
        if stock_name:
            stocks_to_check.append(stock_name)
        for s in stocks_to_check:
            # 仅当唯一个股时才用 code 精确匹配（公告类 name↔code 对应）
            check_code = stock_code if len(stocks_to_check) == 1 else ""
            if _is_hs300_stock(s, check_code, hs300):
                all_non_hs300 = False
                break

    # scope 已在上方计算，此处复用
    is_stock_scope = (scope == "stock")
    if is_stock_scope and has_individual_stock and all_non_hs300 and hs300 is not None:
        # 非沪深300个股：温和降权（仅个股级资讯）
        total = round(total * 0.7, 4)
        # 叠加 ST/退市：强力降权 (0.7 × 0.6 = 0.42)
        if is_st_delist:
            total = round(total * 0.6, 4)
    elif is_st_delist and direction == "bearish":
        # 沪深300的 ST（罕见）或无个股信息的 ST：保留原降权
        total = round(total * 0.85, 4)

    # ---- 自定义关注股票/板块加权 ----
    watchlist = _load_watchlist()
    if watchlist["stocks"] or watchlist["sectors"]:
        hit_watchlist = False
        # 检查个股命中（affected_stocks + stock_name），支持模糊匹配（如"寒武纪-U"匹配"寒武纪"）
        # 短名（<2字）只做精确匹配，避免"AI"等短词误命中含"ai"的任意个股名
        stocks_to_check = list(affected_stocks) + ([stock_name] if stock_name else [])
        for s in stocks_to_check:
            if not s:
                continue
            for watch_stock in watchlist["stocks"]:
                if s == watch_stock:
                    hit_watchlist = True
                    break
                # 模糊匹配仅对长度>=2的名字生效，防止短名子串误命中
                if len(watch_stock) >= 2 and len(s) >= 2 and (watch_stock in s or s in watch_stock):
                    hit_watchlist = True
                    break
            if hit_watchlist:
                break
        # 检查板块命中（模糊匹配：如"算力/AI基础设施/光模块"命中"算力"）
        # 板块名通常>=2字，但仍对单字板块名只做精确匹配
        if not hit_watchlist:
            for sec in news.get("affected_sectors", []) or []:
                for watch_sec in watchlist["sectors"]:
                    if sec == watch_sec:
                        hit_watchlist = True
                        break
                    if len(watch_sec) >= 2 and (watch_sec in sec or sec in watch_sec):
                        hit_watchlist = True
                        break
                if hit_watchlist:
                    break
        if hit_watchlist:
            total = round(total * 1.2, 4)

    # ---- 时间新鲜度加权 ----
    # 将 time_factor 纳入主评分（小幅），让突发新闻在同 band 同分时优先排前
    # 映射区间 0.90~1.00：最新×1.00, 24小时内×0.98, 更早×0.96（最大差异4%）
    tf = calculate_time_factor(news.get("published_at", ""))
    total = round(total * (0.90 + 0.10 * tf), 4)

    # 不在此处封顶，留给 rank_news 在 confidence 加权后统一封顶
    return total


def rank_news(news_list: list) -> list:
    """综合排名：band 主序 → (分数+scope加成) 次序 → 时间因子

    改进：
    1. band 6 档作为主排序键（分级评级优先）
    2. scope 加成融入 total_score 作次排序键（替代原 scope 绝对主键，避免分数倒挂）
    3. confidence 加权（high 1.0 / medium 0.85 / low 0.7）
    4. band 与 direction 冲突时 band 降一档
    5. 沪深300成分股过滤：非沪深300个股资讯降权（入口获取一次，避免循环内重复查询）
    """
    from src.tools.data_fetchers import get_hs300_constituents
    hs300 = get_hs300_constituents()

    ranked = []
    for news in news_list:
        total = _calc_continuous_score(news, hs300)

        conf = news.get("confidence", "medium")
        total = round(total * CONFIDENCE_WEIGHT.get(conf, 0.85), 4)
        # 注意：不再做 0.99 封顶。scope 加成在排序键中统一应用，同 band 内必须让 LLM 价值评分
        # 充分表达差异（原封顶导致 sc=7.0 与 sc=8.5 被压成同一值，产生大量倒挂）。

        band = news.get("impact_band", "neutral")
        direction = news.get("impact_direction", "neutral")
        if _band_direction_conflict(band, direction):
            band = _downgrade_band(band)

        tf = calculate_time_factor(news.get("published_at", ""))

        scope = news.get("influence_scope", "")
        if not scope:
            scope = _infer_influence_scope(news, hs300)

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
            influence_scope=scope,
            analysis_chain=news.get("analysis_chain", ""),
        ))

    ranked.sort(
        key=lambda x: (
            x["band_priority"],                                            # 主序：强信号 > 弱信号 > 中性
            x["total_score"] + SCOPE_SCORE_BOOST.get(x["influence_scope"], 0),  # 次序：分数 + scope 加成
            x["time_factor"],                                              # 时效性微调
        ),
        reverse=True
    )
    return ranked


# ============================================================
# 展示层去重：同股+同事件语义去重 + 同股公告限额
# 供 rank_news_node（Web UI）与 push.py（微信推送）统一调用，
# 避免"去重只在推送层生效、UI/有序输出仍有重复"的不一致。
# ============================================================

# 事件关键词组：同组词命中视为同一事件类型
_EVENT_KEYWORD_GROUPS = [
    ("激励", ["股权激励", "股票激励", "激励计划", "限制性股票"]),
    ("回购", ["回购"]),
    ("增持", ["增持"]),
    ("减持", ["减持"]),
    ("业绩预告", ["业绩预告", "业绩预增", "业绩预减", "业绩快报"]),
    ("并购重组", ["并购", "重组", "收购"]),
    ("定增", ["定增", "非公开发行", "定向增发"]),
    ("分红", ["分红", "派息", "送转", "权益分派"]),
    ("中标签约", ["中标", "签约", "大额订单", "重大合同"]),
    ("立案处罚", ["立案", "处罚", "警示函"]),
    ("退市", ["退市", "终止上市"]),
    ("龙虎榜", ["龙虎榜", "机构净买入"]),
    ("调研", ["投资者关系活动", "调研", "投资者交流"]),
    ("行情下跌", ["走低", "盘前走低", "暴跌", "跌超", "下挫", "重挫", "走弱", "领跌", "跌停", "闪崩", "震荡走弱", "持续走低"]),
]


_MONETARY_RE = re.compile(r'([\d]+(?:\.[\d]+)?)\s*(亿|万)')


def _extract_core_numbers(text: str) -> set:
    """提取金额类核心数字并归一（同事件不同措辞但同金额 → 同事件）

    仅捕获带 亿/万 量词的大额数值（交易额/合同额/募资额等），避免股价、百分比等误命中。
    单位保留在 key 中（"亿:30.53" 与 "万:30.53" 不互相碰撞），
    解决"30.53亿" 与 "30.53亿元" 等同事件不同表述的归一
    （行云科技算力服务补充协议 30.53 亿实证：3 条近重复标题因措辞差异未被合并）。
    """
    out = set()
    for val, unit in _MONETARY_RE.findall(text or ""):
        try:
            v = float(val)
        except ValueError:
            continue
        out.add(f"{unit}:{v:.4g}")
    return out


def _event_signature(news: dict) -> tuple:
    """计算资讯的 (个股集合, 事件组集合, 核心金额集合) 签名"""
    stocks = set(news.get("affected_stocks", []) or [])
    name = news.get("name", "")
    if name:
        stocks.add(name)
    if not stocks:
        # 公告标题多为 "股票名：…"，从标题前缀提取个股（兜底）
        title = news.get("title", "")
        for sep in ("：", ":"):
            if sep in title:
                stocks.add(title.split(sep)[0].strip())
                break
    text = f"{news.get('title', '')} {news.get('content', '')}"
    events = set()
    for group_name, keywords in _EVENT_KEYWORD_GROUPS:
        if any(kw in text for kw in keywords):
            events.add(group_name)
    numbers = _extract_core_numbers(text)
    return stocks, events, numbers


def dedup_ranked_by_event(ranked_news: list) -> list:
    """同股+同事件语义去重：同一 affected_stocks 交集 + (同一事件组 或 同一核心金额) → 保留排名靠前者

    标题 Jaccard 去重无法识别"同一事件的不同角度报道"（标题措辞差异大），
    如"寒武纪股权激励大消息" vs "寒武纪:2026年限制性股票激励计划(草案)摘要公告"。
    本函数用 (个股 ∩ 非空) + (事件组 ∩ 非空 或 核心金额 ∩ 非空) 判定语义重复。

    保守设计：无个股的条目不参与去重；核心金额仅捕获 亿/万 量级大额数值，避免股价/百分比误命中。
    """
    if not ranked_news:
        return ranked_news
    kept = []
    seen_sigs = []
    removed = 0
    for news in ranked_news:
        stocks, events, numbers = _event_signature(news)
        is_dup = False
        if stocks and (events or numbers):
            for ks, ke, kn in seen_sigs:
                if (stocks & ks) and ((events & ke) or (numbers & kn)):
                    is_dup = True
                    break
        if is_dup:
            removed += 1
        else:
            kept.append(news)
            seen_sigs.append((stocks, events, numbers))
    if removed > 0:
        logger.info(f"[display] 同事件去重: {len(ranked_news)} -> {len(kept)}条, 去除{removed}条同股同事件重复")
    return kept


def _announcement_stock_key(news: dict):
    """提取公告涉及的个股键（用于同股公告限额）"""
    stocks = set(news.get("affected_stocks", []) or [])
    name = news.get("name", "")
    if name:
        stocks.add(name)
    if not stocks:
        title = news.get("title", "")
        for sep in ("：", ":"):
            if sep in title:
                stocks.add(title.split(sep)[0].strip())
                break
    return next(iter(sorted(stocks)), None)


def cap_announcements_per_stock(ranked_news: list, max_per_stock: int = 2) -> list:
    """同股公告限额：单只个股的公告最多保留 max_per_stock 条（按当前排序保留靠前者）

    解决实证问题：寒武纪一日发布 9 条公告（5 条股权激励同事件 + 4 条常规），
    事件去重后仍有常规公告噪声，按个股限额收敛为最多 2 条。
    非公告类（news/signal）与无个股归属的公告不参与限额。
    """
    if max_per_stock <= 0:
        return ranked_news
    stock_counts = {}
    capped = []
    removed = 0
    for news in ranked_news:
        if news.get("category") != "announcement":
            capped.append(news)
            continue
        key = _announcement_stock_key(news)
        if key is None:
            capped.append(news)
            continue
        cnt = stock_counts.get(key, 0)
        if cnt >= max_per_stock:
            removed += 1
            continue
        stock_counts[key] = cnt + 1
        capped.append(news)
    if removed > 0:
        logger.info(f"[display] 同股公告限额: 去除{removed}条, 每股最多{max_per_stock}条公告")
    return capped


# 宏观/板块级主题簇主体词：同一主体（机构/国家/商品/地缘事件）的近重复报道应限流，
# 避免"摩根大通美联储推演×3""WTI原油涨×2"等同主体报道霸占头部、稀释 A 股实质利好。
# 仅作用于 market/sector 级；个股级已由同事件去重处理，不再重复限流。
_MACRO_CLUSTER_SUBJECTS = [
    "美联储", "摩根大通", "特朗普", "拜登", "伊朗", "以色列", "欧盟", "欧佩克", "OPEC",
    "WTI", "布伦特", "原油", "黄金", "地缘", "关税", "非农", "CPI", "PPI",
    "俄乌", "中东", "朝鲜", "日本央行", "英国央行", "美债", "美元指数", "加拿大",
]


def _macro_cluster_key(news: dict) -> str | None:
    """提取宏观/板块级资讯的主题簇键（主体词），用于同类报道限流。

    命中首个主体词即返回（不同主体 → 不同簇，不误伤）；无主体词返回 None（不参与限流）。
    """
    text = f"{news.get('title', '')} {news.get('content', '')}"
    for subj in _MACRO_CLUSTER_SUBJECTS:
        if subj in text:
            return subj
    return None


def dedup_macro_clusters(ranked_news: list, max_per_subject: int = 2) -> list:
    """宏观/板块级主题簇限流：同一主体（机构/国家/商品/地缘事件）的近重复报道
    最多保留 max_per_subject 条（保留排名靠前、即综合分更高者），其余沉底/剔除。

    设计取舍：
    - 仅作用于 market/sector 级（influence_scope），个股级（stock）已由同事件去重处理，
      不再重复限流，避免误删仅影响个股自身的资讯。
    - 同主体不同切面（如摩根大通美联储推演的鸽派/鹰派/基准三种分析角度）属于同一事件簇，
      限流后能给 A 股实质利好（永鼎股份订单、兆易创新增持）腾出头部位置。
    - 阈值默认 2：同一主体最多 2 条进展示列表，既保留多角度、又防止霸屏。
    """
    if max_per_subject <= 0:
        return ranked_news
    subject_counts: dict = {}
    capped = []
    removed = 0
    for news in ranked_news:
        scope = news.get("influence_scope", "")
        if scope not in ("market", "sector"):
            capped.append(news)
            continue
        key = _macro_cluster_key(news)
        if key is None:
            capped.append(news)
            continue
        cnt = subject_counts.get(key, 0)
        if cnt >= max_per_subject:
            removed += 1
            continue
        subject_counts[key] = cnt + 1
        capped.append(news)
    if removed > 0:
        logger.info(f"[display] 宏观主题簇限流: 去除{removed}条, 每主体最多{max_per_subject}条")
    return capped


def _extract_title_core(title: str) -> str:
    """提取标题核心内容：去掉【】()等括号内容及标点空格，用于相似度比较"""
    # 防御 None 或非字符串
    title = str(title) if title else ""
    # 去掉【...】、(...)、（...）等括号包裹的前缀/后缀
    t = re.sub(r'[【】\[\]()（）{}<>《》]', '', title)
    # 去掉所有标点和空白
    t = re.sub(r'[^\w\u4e00-\u9fff]', '', t)
    return t


def _title_similarity(t1: str, t2: str) -> float:
    """基于字符集合的 Jaccard 相似度（0-1）"""
    s1, s2 = set(t1), set(t2)
    if not s1 or not s2:
        return 0.0
    return len(s1 & s2) / len(s1 | s2)


def dedup_ranked_by_title(ranked_news: list, threshold: float = 0.6) -> list:
    """排序后标题相似度去重：保留排名靠前的，去掉后续相似标题

    Args:
        ranked_news: 已排序的资讯列表
        threshold: 标题核心内容 Jaccard 相似度阈值，超过则判定重复

    Returns:
        去重后的列表（保持原排序）
    """
    if not ranked_news:
        return ranked_news
    kept = []
    kept_cores = []
    removed = 0
    for news in ranked_news:
        title = news.get("title", "")
        core = _extract_title_core(title)
        is_dup = False
        for kc in kept_cores:
            # 判定重复：Jaccard 相似度高，或短标题核心是长标题核心的子串
            if _title_similarity(core, kc) >= threshold:
                is_dup = True
                break
            # 子串包含：短标题(>=6字)完全包含在长标题中
            shorter, longer = (core, kc) if len(core) <= len(kc) else (kc, core)
            if len(shorter) >= 6 and shorter in longer:
                is_dup = True
                break
        if is_dup:
            removed += 1
        else:
            kept.append(news)
            kept_cores.append(core)
    if removed > 0:
        logger.info(f"[display] 标题相似度去重: {len(ranked_news)} -> {len(kept)}条, 去除{removed}条重复")
    return kept


def dedup_and_cap_for_display(ranked_news: list, max_announcements_per_stock: int = 2, max_per_subject: int = 2) -> list:
    """展示层统一去重：同事件去重 → 标题相似度去重 → 宏观主题簇限流 → 同股公告限额。

    Web UI 与微信推送共用，保证两端展示一致，避免"推送已去重但 UI 仍有重复"。
    标题去重放在事件去重之后，捕获事件去重漏掉的跨源同标题（无个股归属时事件签名无法命中）。
    """
    deduped = dedup_ranked_by_event(ranked_news)
    title_deduped = dedup_ranked_by_title(deduped)
    clustered = dedup_macro_clusters(title_deduped, max_per_subject)
    return cap_announcements_per_stock(clustered, max_announcements_per_stock)


if __name__ == "__main__":
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    news = [
        # 国家级政策利好 — 应该最高
        {"title": "央行降准0.5个百分点释放1万亿", "source": "央视新闻",
         "content": "央行宣布降准降息, 释放流动性", "published_at": now_str,
         "impact_direction": "bullish", "market_impact_score": 9,
         "affected_sectors": ["银行"], "affected_stocks": []},
        # 科技利好
        {"title": "半导体龙头业绩预增涨停", "source": "财联社电报",
         "content": "业绩超预期, 涨停, 半导体板块大涨", "published_at": now_str,
         "impact_direction": "bullish", "market_impact_score": 8,
         "affected_sectors": ["半导体"], "affected_stocks": ["中芯国际"]},
        # 公告类 — 验证精细分级
        {"title": "*ST公司立案调查面临退市风险", "source": "深圳证券交易所",
         "content": "终止上市风险, 立案调查", "published_at": now_str, "category": "announcement",
         "impact_direction": "bearish", "market_impact_score": 9},
        {"title": "某公司业绩预告预增200%", "source": "上海证券交易所",
         "content": "业绩预告, 业绩预增", "published_at": now_str, "category": "announcement",
         "impact_direction": "bullish", "market_impact_score": 8},
        {"title": "*ST公司申请撤销退市风险警示", "source": "深圳证券交易所",
         "content": "撤销退市风险警示, 摘帽", "published_at": now_str, "category": "announcement",
         "impact_direction": "bullish", "market_impact_score": 8},
        {"title": "关于召开2026年第三次临时股东大会的通知", "source": "深圳证券交易所",
         "content": "召开股东大会", "published_at": now_str, "category": "announcement",
         "impact_direction": "neutral", "market_impact_score": 1},
        {"title": "关于投资者关系活动记录表的公告", "source": "上海证券交易所",
         "content": "投资者关系活动, 调研接待", "published_at": now_str, "category": "announcement",
         "impact_direction": "neutral", "market_impact_score": 1},
        # 噪音
        {"title": "某公司举办30周年庆典", "source": "企业自媒体报道",
         "content": "场面盛大", "published_at": now_str},
    ]
    ranked = rank_news(news)
    print("=== 改进后排序验证 ===")
    for i, n in enumerate(ranked, 1):
        cat = "公告" if n["category"] == "announcement" else "新闻"
        print(f"  {i}. [{cat}] {n['title'][:40]}")
        print(f"     信={n['credibility_score']:.2f} 影响={n['market_impact_score']:.1f} 时={n['time_factor']:.2f} 综合={n['total_score']:.4f} [{n['impact_direction']}]")
