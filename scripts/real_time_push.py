# filepath: scripts/real_time_push.py
"""
实时重大资讯推送（事件驱动，只推重要消息）
====================================================
目标: 像财联社公众号一样，有重大消息立刻推送到手机（微信），日常流水不打扰。

数据源（多源聚合，不限于财联社）:
    - get_stock_news: 东财快讯 + 财联社电报 + 新浪财经 + 同花顺快讯
                      + 富途全球快讯 + 华尔街见闻 + 金十数据
                      + Google News（8 源并行抓取 + 跨源去重）
    - get_market_signals: 龙虎榜机构动向 + 业绩预告（交易所官方信号）
    每 30 分钟（云端 GitHub Actions）/ 120 秒（本地 --loop）抓取一轮，
    增量检测（事件级指纹去重）→ 规则预筛 → LLM 严格判定 →
    仅推送重大消息到微信，非重大消息静默丢弃。

判定策略（2026-08-03 用户口径: 全部走 LLM，删除规则降级路径）:
    - 预筛通过的全部候选必须由 LLM 判定推/不推，Python 规则不得直接通关。
    - LLM 未回显 / 批次异常 / 无法解析的条目一律标记 judged=False 挂起
      （不推、不落指纹），留待下一轮重新送 LLM 判定。
    - 已删除 _fallback_decision 规则直推路径（高信号词+高预筛分不再绕过 LLM）。

运行方式:
    云端: GitHub Actions schedule 每 30/60 分钟触发一次
          （见 .github/workflows/realtime-push.yml）
    本地: python scripts/real_time_push.py --loop     # 常驻轮询（守护进程模式）
          python scripts/real_time_push.py --dry-run  # 只诊断不推送不保存状态
          python scripts/real_time_push.py            # 单次执行（等价于一次云端触发）

跨轮次去重（防止重复推送）:
    云端: GitHub Gist（环境变量 GIST_TOKEN + GIST_ID，必须配置，否则 CI 下报错退出）
    本地: logs/real_time_state.json（已被 .gitignore 排除，不入库）
    指纹为事件级: 同事件多源报道（标题措辞不同）共享同一指纹，48h 内只推一次。
    推送级事件签名去重: 指纹分裂的同事件报道（不同金额表述/信号词子集）
    在推送前按(LLM主体+事件组+金额+标题相似)合并，同轮只推最优一条，
    且 48h 内已推过的同事件不再推（恩智浦收购Ambarella三源三推实证修复）。
    Gist 写回为读-改-写合并：本地--loop与云端并发时避免互相覆盖 pushed 标记。

环境变量:
    OPENROUTER_API_KEY / OPENROUTER_MODEL_NAME / OPENROUTER_BASE_URL  LLM 判定（复用现有配置）
    PUSHPLUS_TOKEN     PushPlus token（主渠道，推荐，无需企业微信；pushplus.plus 扫码获取）
    WECOM_WEBHOOK      企业微信群机器人 webhook（可选替代渠道）
    GIST_TOKEN / GIST_ID 云端状态持久化（本地可省）
    RT_PUSH_MODE         strict|standard|loose，重要度门槛，默认 strict
    RT_POLL_SECONDS      本地 --loop 轮询间隔秒数，默认 120
    RT_ALWAYS_ON         1=全天7x24推送，0=仅A股交易日，默认 1
    RT_MAX_CANDIDATES    单轮进入 LLM 判定的候选条数上限，默认 40
"""
import os
import sys
import re
import json
import time
import hashlib
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

BJT = timezone(timedelta(hours=8))

logger = logging.getLogger("real_time_push")

# ============================================================
# 复用项目现有能力（不重复造轮子）
# ============================================================
from src.tools.data_fetchers import get_stock_news, get_market_signals, dedup_news_3layer  # 多源聚合抓取 + 三层近似去重
from src.tools.calculators import (
    calculate_prefilter_importance,   # 预筛评分
    _EVENT_KEYWORD_GROUPS,            # 事件关键词组
    _extract_core_numbers,            # 核心金额提取
    predict_direction_by_rules,       # 规则方向兜底（词表全，含扭亏/退市/涨超/走弱等）
)  # 多源事件签名
from src.tools.push import push_via_wecom, push_via_pushplus  # 推送（含重试）
# LLM 调用与 JSON 修复（2026-08-06 起从共享模块导入，不再依赖废弃的批处理管线 nodes.py）
from src.llm_client import _call_llm_api, _repair_json
from src.tools.keyword_tables import (                      # 共享关键词表（单一事实来源）
    HIGH_SIGNAL_KEYWORDS,
    OVERSEAS_TECH_KEYWORDS,
    OVERSEAS_SOURCE_MARKERS,
    has_signal_keyword,        # 2026-08-06: 词边界感知的信号词匹配（ST/IPO 防误命中）
    find_signal_keywords,      # 返回命中的信号词列表（事件指纹 sig 路径用）
    find_signal_fp_keywords,   # 2026-08-07: 指纹专用信号词（排除宽泛市场词，防跨事件指纹合并）
)

# ============================================================
# 常量配置
# ============================================================
# 高信号关键词（命中则跳过预筛分数限制，直接进入 LLM 判定）：
# 从 src.tools.keyword_tables 导入，与批处理管线 nodes.py 共用同一份，避免漂移。

# 规则预筛门槛：重要度评分 >= 该值 或 命中高信号词 → 进入 LLM 判定
# 多源聚合后候选量远大于单源，门槛从 0.50 提升到 0.55 控住 LLM 成本
PREFILTER_SCORE_MIN = 0.55

# 单轮进入 LLM 判定的候选条数上限（防止突发大行情时候选激增拖慢/超时）
MAX_CANDIDATES_PER_ROUND = 40

# 批量 LLM 判定每批条数（控制单次请求 token 与延迟）
LLM_BATCH_SIZE = 8

# 状态窗口：指纹保留时长（小时），滚动清理
STATE_WINDOW_HOURS = 48

# 候选溢出挂起重试上限（2026-08-06 新增）：同一指纹连续 N 轮溢出后放弃（写 seen），
# 防止突发行情持续超限时 pending 无限累积 / 无限重试消耗 LLM 额度
MAX_PENDING_RETRY = 3

# 心跳告警（2026-08-06 新增）：有新增资讯但连续 N 轮 0 推送时输出告警日志，
# 帮助区分"确实没大事" vs "系统静默故障"
HEARTBEAT_ZERO_PUSH_WARN_ROUNDS = 6
# 进程内计数器（本地 --loop 常驻进程跨轮累计；云端单轮运行无影响）
_zero_push_streak = [0]

# Gist 内状态文件名
GIST_STATE_FILENAME = "real_time_state.json"

# ============================================================
# 阈值模式
# ============================================================
# 外围科技关键词(OVERSEAS_TECH_KEYWORDS)与外围源标记(OVERSEAS_SOURCE_MARKERS)
# 均从 src.tools.keyword_tables 导入（2026-08-01 用户调优版词表已迁入共享模块）。


def _is_overseas_tech(news: dict, sectors: list) -> bool:
    """外围资讯且涉及科技板块 → 增强推送

    美股半导体/科技股/纳指等外围消息，即使 LLM 判定为弱档或个股级，
    只要直接影响 A 股科技板块，也视为值得推送。
    """
    source = str(news.get("source", "") or "")
    if not any(m in source for m in OVERSEAS_SOURCE_MARKERS):
        return False
    text = f"{news.get('title', '')} {news.get('content', '')} {' '.join(str(s) for s in (sectors or []))}"
    return any(kw in text for kw in OVERSEAS_TECH_KEYWORDS)


def _is_domestic_tech(news: dict, sectors: list) -> bool:
    """国内资讯且涉及科技板块 → 增强推送（防 LLM 漏推科技板块资讯）

    用户口径：影响科技板块的资讯不能漏推，但只影响中小市值个股自身的
    业绩/回购/中标等消息即使再轰动也不推。本函数只管"板块级科技影响"——
    调用方需结合 scope==sector（板块级）或 is_leader_stock（科技龙头个股）使用。
    """
    source = str(news.get("source", "") or "")
    if any(m in source for m in OVERSEAS_SOURCE_MARKERS):
        return False
    text = f"{news.get('title', '')} {news.get('content', '')} {' '.join(str(s) for s in (sectors or []))}"
    return any(kw in text for kw in OVERSEAS_TECH_KEYWORDS)


def _passes_threshold(mode: str, score, direction: str, scope: str,
                      leader_stock: bool = False) -> bool:
    """重要度门槛判定（三级模式，默认 strict）

    Args:
        mode: strict / standard / loose
        score: LLM 影响分 0-10
        direction: 6档方向（bullish/bearish=强档；mildly_bullish/
                   mildly_bearish=弱档；neutral/mixed=中性）
        scope: market/sector/stock
        leader_stock: 资讯主体是否为行业龙头个股（LLM 判定 或 命中自选龙头名单）

    Returns:
        是否值得推送

    关键语义（strict 模式）:
    - 全市场级影响: 必推
    - 板块级: 强档方向 或 影响分≥6 → 推（2026-08-01 由≥7放宽：用户反馈大量有用板块资讯被卡）
    - 个股级: 仅行业龙头（龙头股重大消息）且 强档 或 分≥6 → 推；非龙头不推

    注（2026-08-07 注释对齐）：生产入口 run_once 5c 已在调用本函数之前执行
    "强档硬门槛"——direction 非 bullish/bearish 一律不推（2026-08-04 用户口径：
    仅强利好/强利空推送），覆盖 market/sector/stock 全 scope。因此本函数中
    market 必推、sector score>=6、loose 模式等分支在当前生产入口下属于
    保留设计（历史/未来模式扩展用），实际运行时能走到这里的判定必然已是强档。
    """
    try:
        score = float(score or 0)
    except (ValueError, TypeError):
        score = 0.0
    direction = str(direction or "neutral").lower()
    scope = str(scope or "stock").lower()

    # 全市场级影响：任何模式都推
    if scope == "market":
        return True

    strong = direction in ("bullish", "bearish")

    if mode == "strict":
        if scope == "sector":
            return score >= 6 or strong
        if scope == "stock":
            # 龙头个股的重要消息可推；非龙头个股不推
            return leader_stock and (score >= 6 or strong)
        return False
    if mode == "standard":
        # 板块/个股：影响分≥6 或 强档方向
        return score >= 6 or (strong and scope in ("sector", "stock"))
    # loose
    return score >= 5


# ============================================================
# 新闻指纹（跨轮次去重，多源事件级）
# ============================================================
def _normalize_title(title: str) -> str:
    """标题归一化：去标点/空白/emoji，仅保留中英文与数字（用于标题兜底指纹）"""
    t = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", title or "")
    return t[:40]


def _event_signature_light(news: dict) -> tuple:
    """轻量事件签名 (stocks, events, numbers)

    不复用 calculators._event_signature 的 stocks 兜底提取：
    它假设"标题冒号前是股票名"（公告格式），对新闻标题如
    "央行宣布：降准0.5个百分点" 会把"央行宣布"误判为股票，
    导致同一新闻因标点差异（：vs ,）指纹分叉。
    本函数 stocks 仅取显式字段（龙虎榜/业绩预告等自带 name/affected_stocks），
    events/numbers 复用项目的事件关键词组与金额提取。
    """
    stocks = set(news.get("affected_stocks", []) or [])
    name = news.get("name", "")
    if name:
        stocks.add(name)
    text = f"{news.get('title', '')} {news.get('content', '')}"
    events = set()
    for group_name, keywords in _EVENT_KEYWORD_GROUPS:
        if any(kw in text for kw in keywords):
            events.add(group_name)
    numbers = _extract_core_numbers(text)
    return stocks, events, numbers


def _news_fingerprint(news: dict) -> str:
    """多源事件级指纹

    多源聚合下同一事件会被多家媒体报道（标题措辞不同、时间不同），
    不能再按 (时间, 标题) 做指纹。改为两级：
    1. 事件签名（个股+事件组+核心金额）非空 或 命中高信号词
       → 事件级指纹，同事件多源报道共享同一指纹，48h 内只处理/推送一次
    2. 事件签名全空（普通流水新闻）→ 归一化标题指纹
    """
    date = str(news.get("published_at", "") or "")[:10]
    if not date:
        date = datetime.now(BJT).strftime("%Y-%m-%d")
    text = f"{news.get('title', '')} {news.get('content', '')}"
    stocks, events, numbers = _event_signature_light(news)
    # 2026-08-06 修复：find_signal_keywords 用词边界匹配英文缩写（ST/IPO），
    # 防 "STorage" 误命中 ST 导致不同事件指纹合并（漏推）
    # 2026-08-07 修复：改用 find_signal_fp_keywords——排除宽泛市场/机构词
    # （韩国/纳指/央行/油价等，实测"韩国总统宣布新产业计划"与"韩国半导体出口大增"
    # 因共享"韩国"被合并成同一指纹 → 后一条漏推）。仅命中宽泛词时退回
    # 下方标题指纹分支；跨源同事件仍由推送级 _is_same_event 兜底合并。
    hit_signal = find_signal_fp_keywords(text)
    if hit_signal:
        # 命中高信号词（宏观/监管级）：以信号词+事件组+个股为指纹，不掺入数字——
        # 数字表达不稳定（"1700亿" vs "一千七百亿"），且同信号词下数字分叉
        # 会把同一事件的多源报道拆成不同指纹
        key = f"{date}|sig:{sorted(hit_signal)}|ev:{sorted(events)}|st:{sorted(stocks)}"
    elif stocks or events or numbers:
        # 公告/公司事件：个股+事件组+核心金额辅助区分（"XX:回购5亿" vs "XX:回购8亿"）
        key = f"{date}|st:{sorted(stocks)}|ev:{sorted(events)}|num:{sorted(numbers)}"
    else:
        # 普通流水新闻：归一化标题指纹
        key = f"{date}|t:{_normalize_title(news.get('title', ''))}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


# ============================================================
# 推送级事件去重（跨源同事件只推一次）
# ============================================================
def _to_float(v, default: float = 0.0) -> float:
    """宽容转 float（LLM 返回的 score 可能是字符串/None）"""
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return default


def _as_bool(v, default: bool = False) -> bool:
    """严格布尔解析（LLM 返回的 push/is_leader_stock 可能是字符串）

    2026-08-07 修复：此前用 bool(e.get("push"))——LLM 输出 "push": "false"
    （带引号字符串，非标准 JSON）时 bool("false")==True → 误判为要推。
    本函数: bool 直通；数字按真值；字符串按 true/1/yes 等白名单解析，
    无法识别时返回 default（宁可保守不推，也不因类型误判漏推/滥推）。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v or "").strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n"):
        return False
    return default


def _push_event_sig(news: dict, judge: dict) -> dict:
    """生成推送级事件签名：规则抽取(个股/事件组/金额) + LLM主体(entities) + 归一化标题

    指纹 _news_fingerprint 解决"完全同一条"的跨轮去重；本签名解决"同一事件的
    不同报道"（标题措辞/金额表述/信号词子集不同导致指纹分裂，
    恩智浦收购Ambarella三源三推实证）。
    """
    stocks, events, numbers = _event_signature_light(news)
    entities = {str(e).strip() for e in (judge.get("entities") or []) if str(e).strip()}
    sectors = {str(s).strip() for s in (judge.get("sectors") or []) if str(s).strip()}
    return {
        "stocks": sorted(stocks),
        "entities": sorted(entities),
        "events": sorted(events),
        "numbers": sorted(numbers),
        "sectors": sorted(sectors),
        "scope": str(judge.get("scope") or "stock"),
        "title_norm": _normalize_title(news.get("title", "")),
    }


def _merge_event_sig(sig_a: dict, sig_b: dict) -> dict:
    """并集合并两个事件签名（分组内传递合并用），标题保留较长者"""
    return {
        "stocks": sorted(set(sig_a.get("stocks") or []) | set(sig_b.get("stocks") or [])),
        "entities": sorted(set(sig_a.get("entities") or []) | set(sig_b.get("entities") or [])),
        "events": sorted(set(sig_a.get("events") or []) | set(sig_b.get("events") or [])),
        "numbers": sorted(set(sig_a.get("numbers") or []) | set(sig_b.get("numbers") or [])),
        "sectors": sorted(set(sig_a.get("sectors") or []) | set(sig_b.get("sectors") or [])),
        "title_norm": sig_a.get("title_norm", "") if len(sig_a.get("title_norm", "")) >= len(sig_b.get("title_norm", "")) else sig_b.get("title_norm", ""),
    }


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（连续，归一化标题≤40字，DP O(n·m) 开销可忽略）"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _lcs_subseq_len(a: str, b: str) -> int:
    """最长公共子序列长度（允许跨越插入，DP O(n·m)）

    与连续子串 _lcs_len 的区别: 允许中间插入/替换字符仍算匹配，
    可捕获同事件报道中"存储ETF" vs "内存ETF"这类单字差异
    （连续子串 LCS 会因单字不同被拆断，_is_same_event 兜底失效实证）。
    """
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return 0
    prev = [0] * (m + 1)
    for i in range(1, n + 1):
        cur = [0] * (m + 1)
        ai = a[i - 1]
        for j in range(1, m + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = cur[j - 1] if cur[j - 1] >= prev[j] else prev[j]
        prev = cur
    return prev[m]


# 标题方向线索词（LCS 兜底合并的方向对立守卫）
_TITLE_BULLISH_HINTS = ["涨", "升", "利好", "增持", "预增", "突破", "涨停", "上涨", "飙升", "反弹", "回升", "创新高"]
_TITLE_BEARISH_HINTS = ["跌", "降", "利空", "减持", "预减", "跌停", "下跌", "暴跌", "走低", "重挫", "回落", "跳水"]


def _title_direction_conflict(ta: str, tb: str) -> bool:
    """两条归一化标题方向是否对立（防止"集体收涨 vs 集体收跌"被 LCS 误合并漏推）"""
    bull_a = any(k in ta for k in _TITLE_BULLISH_HINTS)
    bear_b = any(k in tb for k in _TITLE_BEARISH_HINTS)
    bear_a = any(k in ta for k in _TITLE_BEARISH_HINTS)
    bull_b = any(k in tb for k in _TITLE_BULLISH_HINTS)
    return (bull_a and bear_b) or (bear_a and bull_b)


# 市场开收盘/复盘类快讯识别（2026-08-03 21:32 美股开盘三源三推实证）
# 该类快讯来自多源（东财/新浪/富途/华尔街）标题措辞差异大、无事件组/实体/金额，
# Jaccard(0.14~0.41) 与 LCS 兜不住 → 按"同时段组 + 同市场域"合并。
_SESSION_GROUPS = {
    "open": ["开盘", "早盘", "盘前"],
    "noon": ["午评", "午盘"],
    "close": ["收盘", "尾盘", "盘后"],
}
_MARKET_DOMAIN_MARKERS = {
    "us": ["美股", "道指", "纳指", "标普", "谷歌", "苹果", "特斯拉", "英伟达"],
    "a": ["A股", "沪指", "深成指", "创业板", "科创50", "沪深"],
    "kr": ["韩股", "韩国综指", "科斯达克"],
    "jp": ["日经", "日股"],
    "hk": ["恒指", "港股"],
    "eu": ["欧股", "欧洲股市"],
}
# 市场指数/基准词（盘中行情动态同事件判定用，2026-08-04 00:32 纳指三推实证）
_INDEX_TOKENS = [
    "纳指", "道指", "标普", "恒指", "日经", "韩指", "韩国综指", "科斯达克",
    "沪指", "深成指", "创业板指", "科创50", "上证", "沪深300", "富时100",
]

# 栏目汇总/盘面播报类噪声识别（2026-08-11 审核实证：48h 内 61 条推送中 13 条为
# 栏目汇总/指数播报/盘面异动类——"晚间新闻精选""隔夜要闻""九点特供""风口研报"
# "KOSPI涨超2%""概念异动拉升"等被 LLM 判强档后仍推送，属"非重大消息"，按用户
# 口径（仅强利好/强利空 + 重大事件）不应推。以下三组规则在 5c 强档门槛后硬过滤，
# 即使 LLM 判强档也降级不推，记录 seen 并标注原因。
# 注意：勿加"早报/晚报"等过宽词——"期货早报"含非农等重大宏观数据，曾实证合理推送。
_NOISE_COLUMN_MARKERS = [
    "新闻精选", "晚间新闻", "要闻速递", "隔夜要闻", "全球要闻", "九点特供", "风口研报",
    "投资避雷针", "午评", "收评", "盘前速递", "新闻速览", "涨停分析",
    "盘前要闻", "市场要闻", "研报精选",
    # 2026-08-12 审核实证漏网："金十数据整理欧盘美盘重要新闻汇总"（8-11 23:32）被推送，
    # 既有词表有"新闻精选"但无"新闻汇总"。补入，含"重要新闻汇总/要闻汇总"等变体。
    # 勿加"早报/晚报"——"期货早报"含非农等宏观数据曾实证合理推送。
    "新闻汇总", "要闻汇总", "市场汇总",
]
# 指数盘中行情播报的波动措辞（与 _INDEX_TOKENS 组合判定，避免误伤个股消息）
_NOISE_INDEX_MOVE = [
    "涨超", "跌超", "涨逾", "跌逾", "涨幅扩大", "跌幅扩大", "涨幅扩大至",
    "跌幅扩大至", "现报", "开涨", "开跌", "收涨", "收跌", "上涨", "下跌",
]
# 纯盘面异动措辞（板块/概念异动、非龙头个股异动；龙头个股消息由 is_leader 例外放行）
_NOISE_INTRADAY_MARKERS = [
    "异动拉升", "直线涨停", "直线跌停", "盘初走强", "盘中走强", "短线走高",
    "震荡拉升", "尾盘拉升", "午后拉升", "冲高回落", "快速走强", "集体走强",
]
# 指数行情播报需额外覆盖的指数名（_INDEX_TOKENS 之外的出现形式）
_NOISE_INDEX_NAMES = _INDEX_TOKENS + ["KOSPI", "恒生指数", "综合指数"]

# 同实体跨日报道的事件短语锚（2026-08-11 修复：索尼×台积电合资 48h 三推实证——
# "拟合资建厂" vs "批准成立合资企业" 实体顺序相反（索尼在前 vs 台积电在前），
# LCS 子序列仅 ~7 字达不到放宽阈值；同实体 + 共享事件动词 → 直接判定同事件。
# 仅保留高置信"事件动词"（合并后必为同一事件）；主题词（扩产/量产/良率/产能/
# 涨价/投资/营收等）易造成不同事件误并（台积电 CoWoS量产 vs 索尼合资 2029量产
# 实证），一律不进锚表，交由放宽 LCS 分支处理。金额守卫：
# 双方金额均明确且无交集 → 不同事件（防"50亿建厂 vs 10亿回购"误并漏推）。）
_EVENT_PHRASE_ANCHORS = [
    "合资", "收购", "并购", "建厂", "成立", "融资", "回购", "入股",
    "签约", "中标", "停牌", "重组", "破产", "退市", "处罚", "立案",
    "增持", "减持",
]


def _session_group(ta: str, tb: str) -> str | None:
    """两条标题是否属于同一市场时段组（开盘组/午评组/收盘组），返回组名或 None"""
    ga = next((g for g, kws in _SESSION_GROUPS.items() if any(k in ta for k in kws)), None)
    gb = next((g for g, kws in _SESSION_GROUPS.items() if any(k in tb for k in kws)), None)
    return ga if (ga is not None and ga == gb) else None


def _market_domain_overlap(ta: str, tb: str) -> bool:
    """两条标题的市场域（美股/A股/韩股/日股/港股/欧股）是否有交集"""
    da = {d for d, kws in _MARKET_DOMAIN_MARKERS.items() if any(k in ta for k in kws)}
    db = {d for d, kws in _MARKET_DOMAIN_MARKERS.items() if any(k in tb for k in kws)}
    return bool(da & db)


def _shared_index_token(ta: str, tb: str) -> bool:
    """两条标题是否共享同一市场指数词（纳指/道指/标普/沪指等）"""
    return any(k in ta and k in tb for k in _INDEX_TOKENS)


def _session_conflict(ta: str, tb: str) -> bool:
    """两条标题是否属于冲突的市场时段（如 午评 vs 收盘）——有时段词且组不同"""
    ga = next((g for g, kws in _SESSION_GROUPS.items() if any(k in ta for k in kws)), None)
    gb = next((g for g, kws in _SESSION_GROUPS.items() if any(k in tb for k in kws)), None)
    return ga is not None and gb is not None and ga != gb


def _entity_overlap(ent_a: set, ent_b: set) -> bool:
    """实体模糊重叠（2026-08-11 修复：跨源同事件合并失败实证）

    LLM 对同一实体的表述不稳定："索尼" vs "索尼半导体解决方案公司"、
    "台积电" vs "台积电(TSM.N)"。仅靠精确相等会把同一事件拆成多条
    （索尼×台积电合资 48h 内三推实证）。判定：
    1. 完全相等
    2. 互相包含（"索尼" ⊂ "索尼半导体解决方案公司"）
    3. 共享连续子串占较短实体 ≥60% 且 ≥2 字（"索尼半导体" vs "索尼"）
    """
    for ea in ent_a:
        for eb in ent_b:
            if not ea or not eb:
                continue
            if ea == eb:
                return True
            if len(ea) >= 2 and len(eb) >= 2 and (ea in eb or eb in ea):
                return True
            m = min(len(ea), len(eb))
            if m >= 2:
                l = _lcs_len(ea, eb)
                if l >= 2 and l / m >= 0.6:
                    return True
    return False


def _strip_entities(text: str, entities: set) -> str:
    """从归一化标题剔除实体词（长词优先），用于"是否除主体外还共享内容"判定

    2026-08-11 修复：实体名重复出现会使 LCS 子序列虚高——
    "央行授权德银" 与 "央行十五五规划" 因"中国人民银行"各出现 1-2 次，
    子序列占比 0.52 被误并。剔除实体后计算共享连续子串可消除该假象。
    """
    for e in sorted(entities, key=len, reverse=True):
        if e:
            text = text.replace(str(e), "")
    return text


def _is_noise_push(news: dict, judge: dict, leader_watchlist: set) -> str:
    """栏目汇总/指数播报/盘面异动类噪声识别（2026-08-11 修复）

    返回噪声原因字符串（非空=应过滤不推），空串=正常条目。
    判定顺序：
    1. 栏目汇总类（新闻精选/要闻速递/九点特供/风口研报/午评/收评等）→ 一律不推
    2. 指数盘中行情播报（指数名 + 涨跌幅度措辞）→ 不推
    3. 板块/概念盘面异动（异动拉升/直线涨停等）→ 非龙头不推；
       LLM 龙头标记或命中自选名单（如"中际旭创跌超3%"）保留
    """
    title = str(news.get("title", "") or "")
    if not title:
        return ""
    if any(m in title for m in _NOISE_COLUMN_MARKERS):
        return "栏目汇总"
    if any(i in title for i in _NOISE_INDEX_NAMES) and any(m in title for m in _NOISE_INDEX_MOVE):
        return "指数播报"
    if any(m in title for m in _NOISE_INTRADAY_MARKERS):
        is_leader = bool(judge.get("is_leader_stock")) or _hit_watchlist(news, leader_watchlist)
        if not is_leader:
            return "盘面异动"
    return ""


# 宏观/政策/地缘类（2026-08-12 修复：防偏科分层配额用——
# 命中此类词条的候选在溢出时优先于普通科技/行情候选，保证宏观不被打压。
# 命中词与 HIGH_SIGNAL_KEYWORDS 的宏观区保持一致，另补少量地缘/数据词。）
_MACRO_POLICY_KEYWORDS = [
    "央行", "证监会", "国常会", "政治局", "国务院", "降准", "降息", "加息",
    "CPI", "PPI", "通胀", "物价", "利率决议", "FOMC", "GDP", "失业率", "就业数据",
    "非农", "美债", "社融", "LPR", "PMI", "印花税", "注册制", "平准基金", "汇金", "国家队",
    "美联储", "鲍威尔", "欧央行", "地缘", "战争", "关税", "贸易战", "制裁", "出口管制",
    "中东", "台海", "原油暴跌", "原油暴涨",
]

# 同题材推送饱和上限（2026-08-12 审核实证：存储/半导体当日推 17 条占 47%，
# 信息价值递减、用户观感"推的都是不重要的"。同一题材（共享板块或实体）
# 24h 内已推 ≥ 上限后，新条目不再推，记 seen 标注；market 级（宏观数据/
# 大盘事件）豁免，保证 CPI 等 market 级数据永不被饱和拦截。
# 上限可通过环境变量 RT_TOPIC_LIMIT 调整。）
TOPIC_PUSH_LIMIT = int(os.getenv("RT_TOPIC_LIMIT", "5"))
TOPIC_PUSH_WINDOW_H = 24.0


def _is_macro_policy(news: dict) -> bool:
    """候选是否属宏观/政策/地缘类（溢出排序优先，防宏观被科技噪声挤占）"""
    title = str(news.get("title", "") or "")
    content = str(news.get("content", "") or "")
    text = f"{title} {content}"
    return any(k in text for k in _MACRO_POLICY_KEYWORDS)


def _topic_saturated(sig: dict, pushed_events: list) -> bool:
    """同题材推送是否已达饱和：market 级豁免；板块/实体 24h 内已推 ≥ 上限"""
    if str(sig.get("scope") or "stock") == "market":
        return False
    secs = set(sig.get("sectors") or [])
    ents = set(sig.get("stocks") or []) | set(sig.get("entities") or [])
    if not secs and not ents:
        return False
    now_ts = time.time()
    cnt = 0
    for pe in pushed_events:
        t = str(pe.get("t") or "")
        try:
            ts = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            continue
        if now_ts - ts > TOPIC_PUSH_WINDOW_H * 3600:
            continue
        psecs = set(pe.get("sectors") or [])
        pents = set(pe.get("stocks") or []) | set(pe.get("entities") or [])
        if (secs & psecs) or (ents & pents):
            cnt += 1
    return cnt >= TOPIC_PUSH_LIMIT


def _is_same_event(sig_a: dict, sig_b: dict) -> bool:
    """判断两个推送级事件签名是否指向同一事件（满足其一即同事件）

    1. 主体(个股/LLM实体)交集非空 且 事件组交集非空 → 同主体同事件
       （"寒武纪股权激励大消息" vs "寒武纪:2026年限制性股票激励计划(草案)"）
    2. 核心金额交集非空 且 事件组交集非空 → 同事件不同措辞同金额
       （"30.53亿补充协议" vs "30.53亿元协议"）
    3. 主体交集为空 但事件组交集非空 且 归一化标题最长公共子串≥5 →
       多源同事件报道（"恩智浦洽谈收购Ambarella" vs "安霸股价因传恩智浦洽谈收购而飙升"）
    4. 双方均无事件组（普通流水）且标题字符集 Jaccard≥0.6 → 同一条目的改写
    5. 双方均无事件组 且 归一化标题 LCS 覆盖较短标题≥55% → 同事件
       （板块/产业资讯兜底：存储ETF纳入、券商研报等 events/entities/numbers 全空，
        Jaccard 因长标题被稀释到 0.4 拦不住，实证花旗研报×2、长鑫ETF×2 同轮双推）
       —— LCS 兜底前先做方向对立守卫，防止涨/跌相反但句式相近的报道被误合并
    6. 双方均无事件组 且 同市场时段域（美股开盘/午评/收盘类多源快讯）→ 同事件
       （2026-08-03 21:32 美股开盘三源三推实证：标题措辞差异大，
        "美股开盘三大股指齐涨" vs "道指开盘涨0.52%" 仅共享'开盘'，
        Jaccard/LCS 均兜不住；按 同时段组 + 同市场域 + 方向不冲突 合并）
    7. 双方均无事件组 且 同市场域 + 共享市场指数词（盘中行情动态）→ 同事件
       （2026-08-04 00:32 纳指三推实证：美股涨幅扩大纳指涨超2 / 纳指涨200现报… /
        纳指涨超2 Meta涨超6 无时段词，Jaccard 0.20~0.33、LCS 占比 0.25~0.46，
        但均含"纳指"且方向一致）
    """
    ent_a = set(sig_a.get("stocks") or []) | set(sig_a.get("entities") or [])
    ent_b = set(sig_b.get("stocks") or []) | set(sig_b.get("entities") or [])
    ev_a = set(sig_a.get("events") or [])
    ev_b = set(sig_b.get("events") or [])
    num_a = set(sig_a.get("numbers") or [])
    num_b = set(sig_b.get("numbers") or [])
    shared_ev = ev_a & ev_b
    # 2026-08-11 修复：实体模糊重叠（"索尼"⊂"索尼半导体解决方案公司"），
    # 此前仅精确相等，跨源同事件（索尼×台积电合资）48h 内三推实证
    ent_overlap = _entity_overlap(ent_a, ent_b)

    if shared_ev and ent_overlap:
        # 2026-08-11 修复（误并实证）：共享事件组 + 实体模糊重叠 直接合并会把
        # "期货早报…连续21月增持黄金"（事件组=增持）与"多重稳市信号释放…"
        # （content 含"增持"事件组）误并为同事件——两者标题零共享。
        # 守卫：标题需共享 ≥3 字连续子串、字符集 Jaccard ≥0.15、或共享高置信
        # 事件短语锚（寒武纪股权激励类：同个股+同事件组+标题同主题 仍合并）。
        ta0 = str(sig_a.get("title_norm", "") or "")
        tb0 = str(sig_b.get("title_norm", "") or "")
        if ta0 and tb0:
            title_ok = (
                _lcs_len(ta0, tb0) >= 3
                or len(set(ta0) & set(tb0)) / len(set(ta0) | set(tb0)) >= 0.15
                or any(p in ta0 and p in tb0 for p in _EVENT_PHRASE_ANCHORS)
            )
            if title_ok:
                return True
        elif ta0 or tb0:
            return True
        return False
    if shared_ev and (num_a & num_b):
        return True
    if shared_ev and not ent_overlap:
        if _lcs_len(sig_a.get("title_norm", ""), sig_b.get("title_norm", "")) >= 5:
            return True
    if not ev_a and not ev_b:
        ta = str(sig_a.get("title_norm", "") or "")
        tb = str(sig_b.get("title_norm", "") or "")
        if ta and tb:
            if _title_direction_conflict(ta, tb):
                return False
            # 市场开收盘/复盘类快讯：同时段组 + 同市场域 → 同事件（多源措辞差异大）
            if _session_group(ta, tb) is not None and _market_domain_overlap(ta, tb):
                return True
            # 盘中行情动态（涨超/现报/涨幅扩大等）：同市场域 + 共享市场指数词
            # + 时段不冲突（防 午评 vs 收盘 因共享指数词误并）→ 同事件
            if (not _session_conflict(ta, tb) and _market_domain_overlap(ta, tb)
                    and _shared_index_token(ta, tb)):
                return True
            jaccard = len(set(ta) & set(tb)) / len(set(ta) | set(tb))
            if jaccard >= 0.6:
                return True
            # 2026-08-11 修复：同实体 + 共享事件短语 → 同事件（跨日报道实体顺序相反、
            # 措辞差异大，LCS 兜不住：索尼×台积电合资三推实证）。金额守卫：
            # 双方金额均明确且无交集 → 不同事件（防"50亿建厂 vs 10亿回购"误并漏推）。
            if ent_overlap and not _title_direction_conflict(ta, tb):
                if (num_a and num_b) and not (num_a & num_b):
                    return False
                if any(p in ta and p in tb for p in _EVENT_PHRASE_ANCHORS):
                    return True
            shorter = min(len(ta), len(tb))
            if shorter >= 8:
                # 2026-08-11 修复：同主体(实体模糊重叠)或同金额时放宽标题相似阈值——
                # 跨日报道措辞差异大（"拟合资建厂" vs "批准成立合资企业"），
                # 但主体+金额一致，放宽 LCS 即可合并，防 48h 内重复推送
                # （韩国5万亿基金×2、索尼台积电合资×3 实证）。
                # 放宽仅对"同实体/同金额"生效：SK海力士不同事件（扩产 vs 股东回报）
                # 标题无共享长段，仍不会被误并（漏推守卫）。
                same_anchor = ent_overlap or bool(num_a & num_b)
                # 误并守卫（2026-08-11 实证）："央行授权德银" vs "央行十五五规划"
                # 仅因实体名"中国人民银行"重复出现，LCS 子序列虚高至 0.52 被误并。
                # 同锚放宽前要求：剔除实体词后标题仍有 ≥3 字连续共享内容，
                # 即两条报道除主体外确实描述同一件事。
                if same_anchor:
                    strip_a = _strip_entities(ta, ent_a)
                    strip_b = _strip_entities(tb, ent_b)
                    if strip_a and strip_b and _lcs_len(strip_a, strip_b) < 3:
                        return False
                # 连续子串兜底（比例足够高 → 同事件）
                if _lcs_len(ta, tb) / shorter >= (0.35 if same_anchor else 0.55):
                    return True
                # 子序列兜底：允许单字替换（"存储ETF" vs "内存ETF"）
                # 仅在匹配长度绝对充足且占比高时启用，避免日常流水误并
                sub = _lcs_subseq_len(ta, tb)
                sub_min = 10 if same_anchor else 12
                sub_ratio = 0.45 if same_anchor else 0.6
                if sub >= sub_min and sub / shorter >= sub_ratio:
                    return True
    return False


# ============================================================
# 状态持久化（Gist 云端 / 本地文件）
# ============================================================
def _empty_state() -> dict:
    return {
        "version": 2,
        "seen": {},  # {fingerprint: {"t": "YYYY-MM-DD HH:MM:SS", "pushed": bool, "title": str}}
        # 候选溢出挂起重试（2026-08-06 新增，防漏推）：
        # 单轮候选超上限被溢出的条目不写 seen（否则 48h 内永久放弃 → 漏推），
        # 而是记入 pending 并在下轮重新进入 LLM 判定。
        # {fingerprint: {"t": str, "retry": int, "title": str}}
        "pending": {},
        # 已推送事件签名（推送级同事件去重，48h 窗口）：
        # [{"t": str, "stocks": [], "entities": [], "events": [], "numbers": [], "title_norm": str}]
        "pushed_events": [],
    }


def _state_path() -> Path:
    return PROJECT_ROOT / "logs" / "real_time_state.json"


def _is_ci() -> bool:
    """GitHub Actions 环境检测（CI=true 由 GitHub 自动注入）"""
    return os.getenv("CI", "").strip().lower() == "true"


def _gist_load(token: str, gist_id: str) -> dict:
    """从 Gist 读取状态文件内容

    注意: GitHub Gist API 对 gist 元数据有 CDN 缓存，写后立即读会命中
    旧快照（实测写入后 1-3 秒甚至更久读不到新内容），本地 --loop 高频
    模式下可能读到写入前版本导致重复推送。
    对策: URL 追加时间戳查询参数强制绕过缓存（实测有效），
    并保留"状态文件未出现则重试"的双保险。
    """
    import requests
    url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stock-news-agent-realtime",
    }
    fobj = None
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            files = data.get("files") or {}
            fobj = files.get(GIST_STATE_FILENAME)
            if fobj is not None:
                break
        except Exception as e:
            logger.warning(f"Gist 读取第{attempt + 1}次失败: {e}")
            fobj = None
        if attempt < 2:
            logger.info(f"Gist 状态文件暂未读到（第{attempt + 1}次，可能命中旧快照），1s 后重试")
            time.sleep(1)
    content = fobj.get("content") if fobj else "{}"
    try:
        state = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Gist 状态文件 JSON 解析失败，重置为空状态")
        state = _empty_state()
    return state


def _gist_save(token: str, gist_id: str, state: dict) -> None:
    """将状态写回 Gist（整文件原子替换，配合 workflow concurrency 防并发）"""
    import requests
    # 与读取一致追加时间戳参数，避免命中 CDN 缓存返回旧元数据
    url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stock-news-agent-realtime",
    }
    payload = {
        "files": {
            GIST_STATE_FILENAME: {
                "content": json.dumps(state, ensure_ascii=False, indent=2),
            }
        }
    }
    # 瞬时网络错误重试（失败会导致已推送标记未落盘 → 下轮重复推送）
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.patch(url, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            return
        except Exception as e:
            last_error = e
            if attempt < 2:
                logger.warning(f"Gist 写入第{attempt + 1}次失败: {e}, 1s 后重试")
                time.sleep(1)
    raise RuntimeError(f"Gist 状态写入失败（已重试2次）: {last_error}")


def load_state() -> dict:
    """加载状态：云端优先 Gist，本地用文件"""
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()

    if gist_token and gist_id:
        try:
            state = _gist_load(gist_token, gist_id)
            state.setdefault("pushed_events", [])
            state.setdefault("pending", {})
            logger.info(f"状态已从 Gist 加载: {len(state.get('seen', {}))} 个指纹, "
                        f"{len(state.get('pushed_events', []))} 个已推事件"
                        f", {len(state.get('pending', {}))} 个挂起重试")
            return state
        except Exception as e:
            logger.warning(f"Gist 读取失败: {e}")
            if _is_ci():
                # CI 下无法读取状态 → 无法去重 → 宁可报错也不冒险重复推送
                raise RuntimeError(f"CI 环境 Gist 读取失败，禁止无状态运行: {e}")

    # 本地文件
    state_path = _state_path()
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or "seen" not in state:
                state = _empty_state()
            state.setdefault("pushed_events", [])
            state.setdefault("pending", {})
            logger.info(f"状态已从本地加载: {len(state.get('seen', {}))} 个指纹, "
                        f"{len(state.get('pending', {}))} 个挂起重试")
            return state
        except Exception as e:
            logger.warning(f"本地状态解析失败，重置: {e}")
    return _empty_state()


def _event_sig_key(e: dict) -> str:
    """事件签名内容键（状态合并去重用）"""
    return ("|".join(sorted(e.get("entities") or [])) + "#"
            + "|".join(sorted(e.get("events") or [])) + "#"
            + "|".join(sorted(e.get("numbers") or [])) + "#"
            + (e.get("title_norm") or ""))


def _merge_state(local: dict, remote: dict) -> dict:
    """合并两份状态（Gist 读-改-写防并发覆盖）：取并集，pushed=True 优先

    本地 --loop 与云端 GitHub Actions 共享同一 Gist 时，两方各自读-改-写，
    直接覆盖写入会丢掉另一方新增的 pushed 标记 → 同一条被重复推送（2026-08-01 实证：
    云端已推送记录被本地轮询覆盖后，Gist 中 pushed=true 记录消失）。
    合并后冲突窗口从"整轮执行"缩小到"单次写入"。
    """
    merged_seen = dict(remote.get("seen", {}) or {})
    for fp, rec in (local.get("seen", {}) or {}).items():
        old = merged_seen.get(fp)
        if old is None or (rec.get("pushed") and not old.get("pushed")):
            merged_seen[fp] = rec
    local["seen"] = merged_seen

    # 合并挂起重试（pending）：retry 计数取较大者（防并发覆盖丢计数）
    merged_pending = dict(remote.get("pending", {}) or {})
    for fp, rec in (local.get("pending", {}) or {}).items():
        old = merged_pending.get(fp)
        if old is None:
            merged_pending[fp] = rec
        else:
            merged_pending[fp] = rec if int(rec.get("retry", 0)) >= int(old.get("retry", 0)) else old
    local["pending"] = merged_pending

    merged_events = {_event_sig_key(e): e for e in (remote.get("pushed_events") or [])}
    for e in (local.get("pushed_events") or []):
        merged_events.setdefault(_event_sig_key(e), e)
    local["pushed_events"] = list(merged_events.values())
    return local


def save_state(state: dict) -> None:
    """保存状态：云端写 Gist（读-改-写合并防并发覆盖），本地写文件"""
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()

    if gist_token and gist_id:
        # 写入前先合并最新远端状态，避免并发实例互相覆盖
        try:
            latest = _gist_load(gist_token, gist_id)
            state = _merge_state(state, latest)
        except Exception as e:
            logger.warning(f"Gist 保存前合并失败（将直接覆盖写入）: {e}")
    elif _is_ci():
        # CI 下没有 Gist 配置 → 状态无处可存 → 下轮会重复推送，必须报错
        raise RuntimeError("CI 环境缺少 GIST_TOKEN/GIST_ID，状态无法持久化，禁止无状态运行")

    # 滚动清理过期指纹（48h 窗口）
    cutoff = (datetime.now(BJT) - timedelta(hours=STATE_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    seen = state.get("seen", {})
    expired = [fp for fp, rec in seen.items() if rec.get("t", "") < cutoff]
    for fp in expired:
        seen.pop(fp, None)
    if expired:
        logger.info(f"清理过期指纹 {len(expired)} 条，剩余 {len(seen)} 条")
    state["seen"] = seen

    # 滚动清理过期挂起重试（48h 窗口）+ 上限 200 条防爆胀
    pending = state.get("pending", {})
    pend_expired = [fp for fp, rec in pending.items() if rec.get("t", "") < cutoff]
    for fp in pend_expired:
        pending.pop(fp, None)
    if len(pending) > 200:
        pending = dict(sorted(pending.items(), key=lambda kv: kv[1].get("t", ""))[-200:])
    state["pending"] = pending
    if pend_expired or len(pending) != len(state.get("pending", {})):
        logger.info(f"清理过期挂起重试 {len(pend_expired)} 条，剩余 {len(pending)} 条")

    # 滚动清理过期已推事件签名（48h 窗口）+ 上限 300 条防爆胀
    pe = [e for e in (state.get("pushed_events") or []) if e.get("t", "") >= cutoff]
    if len(pe) > 300:
        pe = sorted(pe, key=lambda e: e.get("t", ""))[-300:]
    state["pushed_events"] = pe

    if gist_token and gist_id:
        _gist_save(gist_token, gist_id, state)
        logger.info(f"状态已保存到 Gist（{len(seen)} 个指纹, {len(pe)} 个已推事件）")
        return

    state_path = _state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"状态已保存到本地文件: {state_path.name}（{len(seen)} 个指纹）")


# ============================================================
# 规则预筛
# ============================================================
# 宏观数据发布识别（2026-08-12 修复：美国7月CPI系列87条全漏推——
# 根因是 HIGH_SIGNAL_KEYWORDS 缺 CPI/PPI 等词（已补），此为第二层兜底：
# 覆盖"消费者物价指数""生产者出厂价格"等全称变体，以及数据语境快讯。
# 命中 = 宏观数据词 + 数据语境（公布/预期/年率/录得等），确定性事件直接送 LLM 判定。
# 注：数据词用词边界匹配（CPI 不误命中普通文本）；不含数据语境的数据词
# （如"芯片通胀""油价上涨"）不在此直通，仍走常规打分路径。
_MACRO_DATA_PATTERNS = [
    r"(?<![A-Za-z0-9])CPI(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])PPI(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])FOMC(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])GDP(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])PMI(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])LPR(?![A-Za-z0-9])",
    "非农", "失业率", "通胀", "物价", "利率决议", "美债", "社融", "贸易帐",
    "消费者物价", "生产者物价", "出厂价格", "居民消费价格", "工业生产者",
]
_MACRO_RELEASE_CONTEXT = ["公布", "发布", "数据", "预期", "前值", "年率", "月率",
                          "录得", "符合预期", "同比", "环比", "符合市场预期"]


def _is_macro_data_release(text: str) -> bool:
    """宏观数据发布类快讯识别：数据词 + 数据语境 → 直通 LLM 判定"""
    if not text:
        return False
    if not any(re.search(p, text) for p in _MACRO_DATA_PATTERNS):
        return False
    return any(c in text for c in _MACRO_RELEASE_CONTEXT)


def _prefilter(news: dict) -> tuple:
    """规则预筛：返回 (预筛评分, 是否命中高信号词)

    2026-08-12 修复：高信号词命中 或 宏观数据发布识别（_is_macro_data_release）
    均直通 LLM 判定——确定性宏观数据事件即使预筛分低也不允许静默丢弃。
    """
    score = calculate_prefilter_importance(news)
    title = str(news.get("title", "") or "")
    content = str(news.get("content", "") or "")
    name = str(news.get("name", "") or "")
    text = f"{title} {content}"
    # 剥离公司名：避免 "*ST XX" 等公司名命中 "ST" 高信号词直通 LLM 判定
    # （与 nodes._has_high_signal 的处理保持一致）
    if name:
        text = text.replace(name, "")
    # 2026-08-06 修复：改用共享词边界匹配（has_signal_keyword），
    # "STorage/STMicroelectronics" 不再误命中 "ST" 信号词
    hit = has_signal_keyword(text) or _is_macro_data_release(text)
    return float(score), hit


# ============================================================
# LLM 快速重要性判定
# ============================================================
_LLM_SYSTEM_PROMPT = """你是A股资讯重要性审核员。判断每条资讯是否属于"必须立即推送的重大消息"。
推送优先级由高到低，以下任一条件成立则应判为推送：
1. 影响整个市场/大盘（宏观政策、央行、证监会、国常会、政治局会议、重大地缘政治事件；
   以及中国/美国核心宏观数据发布——CPI、PPI、非农、GDP、利率决议、PMI 等——
   数据发布本身即 market 级重大消息，方向按数据对市场的实际含义判定，
   公布后的市场反应（股债汇、加息/降息预期变化、机构点评）同属重大，与数据本体
   合并为一条推送即可，不必每条机构点评都单独推）
2. 影响科技板块/科技产业链的资讯（AI、算力、半导体、芯片、存储、光模块/CPO、PCB、MLCC、
   机器人、消费电子等）：板块景气变化、龙头动向、技术突破、产业政策——即使标题未点名个股
3. 科技龙头个股的重大消息（寒武纪、中际旭创、宁德时代、英伟达产业链相关等第一梯队
   公司的重大经营事件、大额订单、业绩剧变、监管动向）
4. 外围（美股/港股/国际宏观/地缘）消息，若其直接影响A股大盘或科技板块
5. AI/科技龙头的新产品、新模型、新芯片发布（OpenAI/微软/英伟达/谷歌/三星/SK海力士等发布
   新模型版本、自研芯片、HBM新品等）——只要消息经证实或来自权威媒体，即属重大，即使
   没有点名A股公司（2026-08-11 实证漏推：OpenAI发布GPT-5.6-Cyber）
6. 核心科技板块的利空警示（行业见顶信号、龙头目标价被大幅下调、产能过剩担忧、重大诉讼/
   监管审查）——利空警示同样属于必须推送的重大消息，方向为 bearish（2026-08-11 实证漏推：
   韩国券商砍三星/SK海力士目标价约30%）
7. AI 监管与政策（立法机构、监管机构、政府要员对 AI 开发/使用/出口的限制、调查、听证）——
   即使不点名具体公司（2026-08-11 实证漏推：美参议员致信要求暂停AI开发）
明确不推（无论业绩多好、涨跌多剧烈）：
- 纯个人观点/猜测类言论：政客或机构单方面"怀疑""认为""预计"等表态，没有真实事件或官方立场
  变化、没有实际市场反应佐证，则不视为重大事件——即便话题涉及地缘、石油或美股
- 只影响中小市值个股自身股价的消息：业绩预告/业绩变动、小额回购、增持/减持、中标/签约、
  日常经营、子公司事项、分红送转等——除非该股是行业龙头或直接改变板块逻辑
- 外围央行（非中国）的日常表态/会议纪要/储备数据（日本央行委员意见、印度央行、匈牙利央行等），
  除非涉及重大政策转向或直接影响 A 股；注意：此条不含宏观数据发布本身——
  外围重要数据（美国 CPI/PPI/非农/利率决议等）因直接影响全球市场与 A 股，仍按优先级 1 推送
- 分析师评级调整/目标价小幅变动（杰富瑞、伯恩斯坦等），除非幅度极大且已引发市场剧烈反应
- 无官方确认的产品传闻/路线图预测（苹果折叠 iPhone、郭明錤预测等）
- 与上述优先级均无关的其他资讯

对每条输入严格输出一个 JSON 数组元素，字段：
{"idx": 输入的idx原样回显, "title": "原标题", "push": true/false, "score": 0到10的整数, "direction": "bullish|mildly_bullish|neutral|mixed|mildly_bearish|bearish",
 "scope": "market|sector|stock", "sectors": ["板块名"], "entities": ["事件主体公司/机构规范简称，1-3个，无则空数组"], "is_leader_stock": true/false,
 "reason": "一句话理由"}
direction 必须区分强度：只有影响显著且方向明确才用 bullish/bearish（强档）；
小幅波动用 mildly_bullish/mildly_bearish；方向不明用 neutral/mixed。
is_leader_stock: 仅当该资讯主体是行业龙头个股（市值/地位第一梯队）时为 true，否则 false。
entities: 事件的当事公司/机构/人物规范简称（如"恩智浦""安霸""美联储"），用于跨源同事件去重，必须与标题所述主体一致。
注意: 输出必须是合法 JSON，字符串内的双引号必须转义为 \"（或改用「」）；idx 必须原样回显。
不要输出任何 JSON 以外的文字。"""


def _build_llm_user_prompt(items: list) -> str:
    lines = []
    for n in items:
        lines.append(json.dumps({
            "idx": n.get("_judge_idx"),
            "title": str(n.get("title", ""))[:80],
            "content": str(n.get("content", "") or "")[:200],
            "published_at": str(n.get("published_at", "")),
        }, ensure_ascii=False))
    return "请逐条审核以下资讯（不要遗漏任何一条，idx 原样回显）:\n[\n" + ",\n".join(lines) + "\n]"


def _rescue_judge_object(obj_text: str) -> dict | None:
    """从损坏的 JSON 对象文本中抢救判定字段（应对中文引号未转义等）

    例: {"title": "美联储巴尔金：利率是否已足够高是个"悬而未决"的问题", "push": false, ...}
    json.loads 与 _repair_json 均失败时，用正则直接抽取关键字段，
    避免整条判定被丢弃（漏推根因之一，2026-08-01 日志实证）。
    """
    m_push = re.search(r'"push"\s*:\s*(true|false)', obj_text, re.I)
    m_score = re.search(r'"score"\s*:\s*(\d+(?:\.\d+)?)', obj_text)
    if not m_push or not m_score:
        return None

    def _str_field(name: str, default: str = "") -> str:
        m = re.search(rf'"{name}"\s*:\s*"([^"]*)"', obj_text)
        return m.group(1) if m else default

    out = {
        "push": m_push.group(1).lower() == "true",
        "score": float(m_score.group(1)),
        "direction": _str_field("direction", "neutral"),
        "scope": _str_field("scope", "stock"),
        "reason": _str_field("reason", ""),
    }
    m_idx = re.search(r'"idx"\s*:\s*(\d+)', obj_text)
    if m_idx:
        out["idx"] = int(m_idx.group(1))
    # title 允许内含未转义引号：非贪婪匹配到第一个", "为止
    m_title = re.search(r'"title"\s*:\s*"(.+?)"\s*,', obj_text)
    if m_title:
        out["title"] = m_title.group(1)
    return out


def _parse_llm_array(content: str) -> list:
    """解析 LLM 返回的 JSON 数组（容错：代码块包裹/损坏对象/截断）

    注意: 不复用 nodes._safe_parse_json —— 它的容错逻辑只为 filtered_news/
    ranking 等 dict 结构设计，裸数组场景会全部 fallback 失败。
    本函数: 提取代码块 → 逐字符扫描平衡括号 → 逐对象解析
    （先 _repair_json 兜底，再 _rescue_judge_object 正则抢救）。
    """
    if not content or not content.strip():
        return []
    cleaned = content
    # 提取 ```json ... ``` 代码块
    cb_match = re.search(r'```(?:json)?\s*\n?(.*?)```', cleaned, re.DOTALL)
    if cb_match:
        cleaned = cb_match.group(1)
    # 定位数组起点（防御 LLM 在数组前输出解释文字）
    start = cleaned.find("[")
    if start < 0:
        return []

    items = []
    i = start
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
        if ch == "\\":
            escape = True
            i += 1
            continue
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                if obj_depth == 0:
                    obj_start = i
                obj_depth += 1
            elif ch == "}":
                obj_depth -= 1
                if obj_depth == 0 and obj_start >= 0:
                    obj_text = cleaned[obj_start:i + 1]
                    try:
                        items.append(json.loads(obj_text))
                    except json.JSONDecodeError:
                        try:
                            items.append(json.loads(_repair_json(obj_text)))
                        except json.JSONDecodeError:
                            rescued = _rescue_judge_object(obj_text)
                            if rescued is not None:
                                items.append(rescued)
                            else:
                                logger.warning(f"LLM 返回单对象解析失败，跳过: {obj_text[:60]}")
                    obj_start = -1
        i += 1
    return items


def _hang_judge(news: dict) -> dict:
    """LLM 未判定的挂起状态：不推、不落指纹，留待下一轮重新送 LLM 判定。

    2026-08-03 用户口径：删除规则降级路径，预筛通过的全部候选必须由 LLM
    判定推/不推，Python 规则不得直接通关。judged=False 的条目在 run_once
    中被跳过且不写入 seen 指纹，因此下轮会重新进入 LLM 判定。
    """
    return {
        "title": str(news.get("title", "") or ""),
        "push": False,
        "judged": False,
        "score": 0,
        "direction": "neutral",
        "scope": "stock",
        "sectors": [],
        "entities": [],
        "is_leader_stock": False,
        "reason": "LLM未判定，挂起下轮重试",
    }


def _llm_judge(items: list, deadline: float = 0) -> list:
    """批量 LLM 判定，返回与 items 一一对应的判定 dict 列表

    Args:
        items: 待判定候选
        deadline: 总超时熔断时间戳(time.monotonic())，0=不限。
                  2026-08-06 修复：此前调用 _call_llm_api 未传 deadline，
                  多批(最多5批)×90s×2次重试最坏 900s，突破 GitHub Actions
                  timeout-minutes:10 导致任务被强杀（已推送未保存状态→重复推送）。
                  现在批次循环入口检查熔断，逼近 deadline 立即挂起剩余批次。

    对齐策略: 给每条注入 _judge_idx 并要求 LLM 回显 idx，按 idx 精确合并
    （标题匹配仅作兜底）。标题精确匹配不可靠——LLM 会改写/截断标题，
    批处理管线实证 60 条仅 26 条标题精确命中，未对齐时会被误判不推，
    是漏推的主要根因。

    规则直通限制（2026-08-03 用户口径）: 预筛通过的全部候选必须由 LLM
    判定推/不推，Python 规则不得直接通关。因此 LLM 未回显 / 批次异常 /
    无法解析的条目一律标记 judged=False 挂起（push=False, score=0），
    由 run_once 跳过、不落指纹，留待下一轮重新送 LLM 判定。
    已删除 _fallback_decision 规则直推路径。
    """
    if not items:
        return []
    results = [None] * len(items)
    for start in range(0, len(items), LLM_BATCH_SIZE):
        batch = items[start:start + LLM_BATCH_SIZE]
        # 总超时熔断：逼近 deadline 剩余批次全部挂起下轮重试（避免突破 Actions 10min 上限）
        if deadline and time.monotonic() >= deadline:
            logger.warning(f"LLM 判定逼近总超时熔断，批次 {start//LLM_BATCH_SIZE + 1} 起共 "
                           f"{len(items) - start} 条挂起下轮重试")
            for offset, n in enumerate(batch):
                results[start + offset] = _hang_judge(n)
            for remaining in range(start + LLM_BATCH_SIZE, len(items)):
                results[remaining] = _hang_judge(items[remaining])
            break
        for offset, n in enumerate(batch):
            n["_judge_idx"] = start + offset
        try:
            raw = _call_llm_api(_LLM_SYSTEM_PROMPT, _build_llm_user_prompt(batch), timeout=90, max_retries=1, deadline=deadline)
            entries = _parse_llm_array(raw)
            # 按 idx 精确对齐（标题兜底）：LLM 可能增减条目/乱序/改写标题
            by_idx = {}
            by_title = {}
            for e in entries:
                if not isinstance(e, dict):
                    continue
                try:
                    i = int(e.get("idx", -1))
                    if 0 <= i < len(items):
                        by_idx[i] = e
                except (ValueError, TypeError):
                    pass
                t = str(e.get("title", "") or "").strip()
                if t:
                    by_title[t] = e
            for offset, n in enumerate(batch):
                i = start + offset
                t = str(n.get("title", "") or "").strip()
                e = by_idx.get(i) or by_title.get(t)
                if not e:
                    # LLM 成功返回但未回显该条目（截断/遗漏/标题改写导致 idx 与标题均未对齐）。
                    # 不静默判 push=False（避免把可推重大消息漏掉），也不规则直推——
                    # 挂起留待下轮重新送 LLM 判定。
                    logger.warning(f"LLM 未回显条目 idx={i}，挂起下轮重试: {t[:40]}")
                    results[i] = _hang_judge(n)
                    continue
                results[i] = {
                    "title": t,
                    "push": _as_bool(e.get("push", False), False),
                    "judged": True,
                    "score": e.get("score", 0),
                    "direction": _normalize_direction(e.get("direction", "neutral"), n),
                    "scope": str(e.get("scope", "stock") or "stock").lower(),
                    "sectors": e.get("sectors") or [],
                    "entities": [str(x).strip() for x in (e.get("entities") or []) if str(x).strip()],
                    "is_leader_stock": _as_bool(e.get("is_leader_stock", False), False),
                    "reason": str(e.get("reason", "") or "").strip(),
                }
            logger.info(f"LLM 判定批次 {start//LLM_BATCH_SIZE + 1}: {len(batch)} 条完成（回显{len(entries)}条）")
        except Exception as e:
            logger.warning(f"LLM 判定批次失败（{len(batch)} 条），整批挂起下轮重试: {e}")
            for offset, n in enumerate(batch):
                results[start + offset] = _hang_judge(n)
    # 防御：任何未填充位置挂起（不落指纹，下轮重试）
    for i, r in enumerate(results):
        if r is None:
            n = items[i]
            results[i] = _hang_judge(n)
    return results


def _load_leader_watchlist() -> set:
    """加载自选龙头名单（watchlist.json 的 stocks），与 LLM 判定互为补充"""
    try:
        wl = json.loads((PROJECT_ROOT / "watchlist.json").read_text(encoding="utf-8"))
        stocks = wl.get("stocks", []) or []
        names = set()
        for s in stocks:
            if isinstance(s, str):
                names.add(s.strip())
            elif isinstance(s, dict):
                names.add(str(s.get("name", "") or "").strip())
        return {n for n in names if n}
    except Exception:
        return set()


def _hit_watchlist(news: dict, watchlist: set) -> bool:
    """资讯主体是否命中自选龙头名单（标题/名称/代码匹配）"""
    if not watchlist:
        return False
    title = str(news.get("title", "") or "")
    name = str(news.get("name", "") or "")
    content = str(news.get("content", "") or "")
    for n in watchlist:
        if n and (n in title or n in name or n in content):
            return True
    return False


def _normalize_direction(value, news: dict = None) -> str:
    """归一化 LLM 返回的方向值到 6 档标准值

    问题: LLM 输出不稳定，direction 可能返回非标准值（"bullish "带空格、
    中文"看涨"、"强利好"、乱码等），直接进 _passes_threshold 会丢失
    bullish/bearish 强档位 → 板块/个股资讯被误降级为"不推"（用户反馈：
    利好利空被降级、判断不准）。
    本函数: 标准值直通；常见别名归一；无法识别时用规则方向兜底
    predict_direction_by_rules（词表更全）而非一律 neutral。

    2026-08-03 修复（用户反馈"利空显示利好"）:
    - 混合方向检测优先：含"涨"又含"跌/回落"（先涨后跌/涨后回落/冲高回落/
      利好兑现后回落等）→ 判 mixed，不再因"涨/升"别名先于"跌/降"命中而误判 bullish。
    - 别名子串匹配改为按 key 长度降序（长别名优先），避免"弱利好"被"利好"抢先。
    """
    if value is None:
        value = ""
    raw = str(value).strip().lower()
    if raw in ("bullish", "mildly_bullish", "bearish", "mildly_bearish", "neutral", "mixed"):
        return raw
    # 混合方向检测：同一字符串中既有多头又有多空信号 → mixed
    _BULL_MARKERS = ("涨", "升", "利好", "看多", "看涨", "偏多", "走强", "上行")
    _BEAR_MARKERS = ("跌", "降", "利空", "看空", "看跌", "偏空", "走弱", "回落", "下行")
    if any(k in raw for k in _BULL_MARKERS) and any(k in raw for k in _BEAR_MARKERS):
        return "mixed"
    alias_map = {
        "看涨": "bullish", "看多": "bullish", "利多": "bullish", "利好": "bullish",
        "强利好": "bullish", "偏多": "bullish", "强势": "bullish", "positive": "bullish",
        "涨": "bullish", "升": "bullish",
        "看跌": "bearish", "看空": "bearish", "利淡": "bearish", "利空": "bearish",
        "强利空": "bearish", "偏空": "bearish", "弱势": "bearish", "negative": "bearish",
        "跌": "bearish", "降": "bearish",
        "弱利好": "mildly_bullish", "小幅利好": "mildly_bullish",
        "弱利空": "mildly_bearish", "小幅利空": "mildly_bearish",
        "多空交织": "mixed", "中性": "neutral", "无影响": "neutral",
        "中性偏多": "mildly_bullish", "中性偏空": "mildly_bearish",
    }
    if raw in alias_map:
        return alias_map[raw]
    # 别名内嵌（如 "Mildly Bullish"、"偏多中性"）：按关键词子串匹配，
    # key 按长度降序——长别名（弱利好/中性偏多）优先于单字（利好/涨/跌）
    for key, mapped in sorted(alias_map.items(), key=lambda kv: len(kv[0]), reverse=True):
        if key and key in raw:
            return mapped
    if raw.startswith("mildly"):
        return raw if raw in ("mildly_bullish", "mildly_bearish") else "neutral"
    # 无法识别：用规则方向兜底（比一律 neutral 更准）
    if news is not None:
        rule_dir = predict_direction_by_rules(
            str(news.get("title", "") or ""), str(news.get("content", "") or ""))
        if rule_dir in ("bullish", "bearish"):
            return rule_dir
    return "neutral"


# ============================================================
# 推送格式 + 统一推送入口
# ============================================================
_DIR_EMOJI = {
    "bullish": "🔴", "mildly_bullish": "🟠",
    "bearish": "🟢", "mildly_bearish": "🟡",
    "neutral": "⚪", "mixed": "🔷",
}
_DIR_LABEL = {
    "bullish": "强利好", "mildly_bullish": "弱利好",
    "bearish": "强利空", "mildly_bearish": "弱利空",
    "neutral": "中性", "mixed": "多空交织",
}
_SCOPE_LABEL = {"market": "全市场", "sector": "板块", "stock": "个股"}


def format_push_alert(news: dict, judge: dict) -> str:
    """格式化单条快讯为 markdown 文本（红涨绿跌）

    注: 企业微信 markdown 不支持 <font color> 内联 HTML，
    统一用 emoji + 文本标签表达方向（A股惯例红涨绿跌），
    PushPlus / 企业微信均能正确渲染。

    正文处理: 资讯原文 content 截断 300 字符附在推送里（避免只推标题+理由
    让用户看不到资讯内容），企业微信 4096 字节限制内有充足余量。
    """
    title = str(news.get("title", "") or "")[:200]
    direction = judge.get("direction", "neutral")
    emoji = _DIR_EMOJI.get(direction, "⚪")
    label = _DIR_LABEL.get(direction, "中性")
    score = judge.get("score", 0)
    scope = judge.get("scope", "stock")
    scope_label = _SCOPE_LABEL.get(scope, "个股")
    sectors = judge.get("sectors") or []
    sector_str = "、".join(str(s) for s in sectors[:4]) if sectors else "—"
    reason = str(judge.get("reason", "") or "").strip()[:100]
    source = str(news.get("source", "") or "多源资讯")
    pub = str(news.get("published_at", "") or "")
    content_text = str(news.get("content", "") or "").strip()

    lines = [f"{emoji}【{label}】{title}", ""]
    meta = [f"**范围**: {scope_label}", f"**影响分**: {score}", f"**板块**: {sector_str}"]
    lines.append(" | ".join(meta))
    lines.append(f"**来源**: {source} {pub}")
    if content_text:
        body = content_text[:300]
        if len(content_text) > 300:
            body += "..."
        lines.append("")
        lines.append(f"> 📄 {body}")
    if reason:
        lines.append("")
        lines.append(f"> {reason}")
    return "\n".join(lines)


def _send_alert_item(push_config: dict, title: str, content: str) -> dict:
    """统一推送入口：按配置选择后端发送单条快讯

    后端优先级: PushPlus > 企业微信群机器人
    成功判定: PushPlus code==200; 企业微信 errcode==0
    """
    if push_config.get("pushplus_token"):
        return push_via_pushplus(push_config["pushplus_token"], title, content)
    if push_config.get("wecom_webhook"):
        return push_via_wecom(push_config["wecom_webhook"], title, content)
    return {"code": 400, "msg": "未配置任何推送后端"}


# ============================================================
# 主流程：单次执行
# ============================================================
def run_once(dry_run: bool = False) -> dict:
    """执行一轮实时推送

    Args:
        dry_run: True 时不推送、不保存状态（用于本地诊断）

    Returns:
        本轮统计 dict
    """
    pushplus_token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    wecom_webhook = os.getenv("WECOM_WEBHOOK", "").strip()
    if not pushplus_token and not wecom_webhook and not dry_run:
        raise RuntimeError("未配置推送后端: 需要 PUSHPLUS_TOKEN（推荐，pushplus.plus 扫码获取）或 WECOM_WEBHOOK，请先配置")
    push_config = {
        "pushplus_token": pushplus_token or None,
        "wecom_webhook": wecom_webhook or None,
    }

    mode = os.getenv("RT_PUSH_MODE", "strict").strip().lower()
    if mode not in ("strict", "standard", "loose"):
        logger.warning(f"RT_PUSH_MODE 无效: {mode}，使用 strict")
        mode = "strict"

    state = load_state()
    seen = state.setdefault("seen", {})
    pending = state.setdefault("pending", {})

    # 1. 多源聚合抓取：6 大新闻源 + 龙虎榜/业绩预告信号
    # 注意: get_stock_news/get_market_signals 是 LangChain @tool 包装的
    # StructuredTool 实例，需用 .func 取原始函数调用
    # 2026-08-07 修复：新闻与信号分开 try，单类失败不清空另一类成功数据
    # （此前共用 try，signals 抛异常会把已成功的 news_list 也置空 → 误判"无资讯"）
    try:
        news_list = get_stock_news.func()
    except Exception as e:
        logger.error(f"多源新闻抓取失败: {e}", exc_info=True)
        news_list = []
    try:
        signals = get_market_signals.func()
    except Exception as e:
        logger.error(f"市场信号抓取失败: {e}", exc_info=True)
        signals = []

    # 跨源近似去重（URL/精确标题之外补一层 SimHash）：同一事件不同措辞的多源
    # 报道先在入口收敛，避免各自进指纹/候选、重复消耗 LLM 判定 token。
    # 注: 只作用于多源新闻(dedup_news_3layer 对标题 SimHash)，signals 是交易所
    # 结构化数据（龙虎榜/业绩预告）且已按代码内去重，模板化标题不参与近似去重，
    # 避免"业绩预告: XX(代码) 预增 幅度+X%" 同模板不同股票被 SimHash 误判重复。
    before_dedup = len(news_list)
    news_list = dedup_news_3layer(list(news_list))
    if len(news_list) < before_dedup:
        logger.info(f"三层近似去重: {before_dedup} -> {len(news_list)} 条")
    news_list = news_list + list(signals)
    logger.info(f"多源聚合: 拉取 {len(news_list)} 条")
    if not news_list:
        logger.info("无资讯返回，跳过本轮")
        return {"fetched": 0, "new": 0, "prefiltered": 0, "pushed": 0, "skipped": 0}

    # 2. 增量检测：事件级指纹去重，只处理未见过指纹的条目
    # pending 中的溢出条目允许重新进入（防漏推：候选溢出不再永久 seen）
    new_items = []
    for n in news_list:
        fp = _news_fingerprint(n)
        if fp in seen:
            continue
        n["_fp"] = fp
        n["_pend_retry"] = int((pending.get(fp) or {}).get("retry", 0))
        new_items.append(n)
    logger.info(f"增量检测: 新增 {len(new_items)} 条（已见 {len(news_list) - len(new_items)} 条）")
    if not new_items:
        logger.info("无新增资讯，本轮结束")
        save_state(state)  # 触发窗口清理
        return {"fetched": len(news_list), "new": 0, "prefiltered": 0, "pushed": 0, "skipped": 0}

    # 3. 规则预筛（重要度评分 或 高信号词命中）
    candidates = []
    for n in new_items:
        pref_score, hit = _prefilter(n)
        n["_pref_score"] = pref_score
        n["_hit_signal"] = hit
        if pref_score >= PREFILTER_SCORE_MIN or hit:
            candidates.append(n)
    # 候选上限保护：突发大行情候选激增时，按预筛分排序取前 N 条
    # 2026-08-06 修复（P1-2）：溢出条目不写 seen（此前被末尾循环永久 seen → 48h 漏推），
    # 改为记入 pending 挂起，下轮重新进入增量检测与 LLM 判定。
    # 2026-08-07 修复（分层配额）：此前仅按 _pref_score 全局截断——预筛分混合了
    # 科技/板块/来源偏好，极端行情下"立案调查/降准"等高信号核心事件可能被
    # 低分科技噪声挤出第 41 位。现改为两级排序：命中高信号词（核心事件）恒优先，
    # 普通候选再按预筛分排序，保证核心事件在溢出时不被挤占。
    # 放弃策略同步收紧：仅普通条目连续 MAX_PENDING_RETRY 轮溢出后放弃（写 seen 防无限重试）；
    # 高信号条目永不放弃（持续挂起 pending，行情回落后自动进入判定）——核心事件不许漏推。
    max_candidates = int(os.getenv("RT_MAX_CANDIDATES", str(MAX_CANDIDATES_PER_ROUND)))
    overflow = []
    if len(candidates) > max_candidates:
        # 2026-08-12 修复（防偏科分层）：高信号核心事件恒优先 → 宏观/政策/地缘类
        # 次优先 → 普通候选按预筛分。此前"高信号>预筛分"两级排序中，同为高信号的
        # 科技条目（韩国/存储/英伟达等宽泛词也命中）仍可能把宏观数据挤出——
        # 今日 36 推中存储 17 条、宏观 0 条实证。宏观优先确保 CPI/降准等
        # 确定性宏观事件在溢出时不被打压。
        candidates.sort(
            key=lambda x: (
                1 if x.get("_hit_signal") else 0,
                1 if _is_macro_policy(x) else 0,
                x["_pref_score"],
            ),
            reverse=True)
        overflow = candidates[max_candidates:]
        candidates = candidates[:max_candidates]
        now_for_pend = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
        for n in overflow:
            retry = int(n.get("_pend_retry", 0)) + 1
            if retry >= MAX_PENDING_RETRY and not n.get("_hit_signal"):
                # 连续多轮溢出：放弃普通条目，记 seen 防无限重试
                # （高信号核心事件不放弃——漏推立案调查/降准等比多一轮重试代价大得多）
                seen[n["_fp"]] = {"t": now_for_pend, "pushed": False,
                                  "title": str(n.get("title", ""))[:60] + "[溢出放弃]"}
                logger.info(f"候选溢出重试{retry}轮仍无法进入判定，放弃: {n.get('title', '')[:40]}")
            else:
                pending[n["_fp"]] = {"t": now_for_pend, "retry": retry,
                                     "title": str(n.get("title", ""))[:60]}
        logger.info(f"候选超过上限({max_candidates})，溢出 {len(overflow)} 条进入挂起重试")
        overflow_fps = {n["_fp"] for n in overflow}
    else:
        overflow_fps = set()
    logger.info(f"规则预筛: 通过 {len(candidates)}/{len(new_items)} 条")
    if not candidates:
        # 全部不达标：记录指纹，不推送（跳过 pending 溢出条目——它们下轮重试）
        now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
        for n in new_items:
            if n["_fp"] in pending:
                continue
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}
        if not dry_run:
            save_state(state)
        return {"fetched": len(news_list), "new": len(new_items), "prefiltered": 0, "pushed": 0, "skipped": len(new_items)}

    # 4. LLM 严格判定（全部候选必须经 LLM 判定，无规则降级路径）
    # 总超时熔断（2026-08-06）：本轮 LLM 判定最多 300s，超过则剩余批次挂起下轮重试。
    # 防止多批×重试叠加突破 GitHub Actions timeout-minutes:10（此前最坏 900s）。
    _llm_deadline = time.monotonic() + 300
    judges = _llm_judge(candidates, deadline=_llm_deadline)
    if not judges:
        # 防御：_llm_judge 异常返回空 → 全部挂起下轮重试（不推、不落指纹）
        judges = [_hang_judge(n) for n in candidates]

    # 自选龙头名单（watchlist.json），与 LLM 的 is_leader_stock 判定互为补充
    leader_watchlist = _load_leader_watchlist()

    # 5. 同事件合并（跨源同事件只推最优一条）→ 跨轮已推事件拦截 → 阈值过滤 → 推送
    pushed = 0
    skipped = 0
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")

    # 5a. 生成推送级事件签名并按同事件分组：指纹解决"同一条"，签名解决"同一事件的不同报道"
    groups = []
    for n, j in zip(candidates, judges):
        if not isinstance(j, dict):
            j = _hang_judge(n)
        sig = _push_event_sig(n, j)
        n["_sig"] = sig
        for g in groups:
            if _is_same_event(sig, g["sig"]):
                g["items"].append((n, j))
                g["sig"] = _merge_event_sig(g["sig"], sig)  # 传递合并，链式同事件也能汇聚
                break
        else:
            groups.append({"sig": sig, "items": [(n, j)]})
    if len(groups) < len(candidates):
        logger.info(f"同事件合并: {len(candidates)}条候选 -> {len(groups)}个事件 "
                    f"(合并{len(candidates) - len(groups)}条多源重复)")

    # 5b. 每组选最优代表（LLM判定可推者优先，其次影响分/预筛分高者），落选者记录指纹不再处理
    reps = []
    for g in groups:
        best = max(g["items"], key=lambda x: (
            bool(x[1].get("push")),
            _to_float(x[1].get("score", 0)),
            x[0].get("_pref_score", 0),
        ))
        reps.append(best)
        for n, _j in g["items"]:
            if n is not best[0]:
                seen[n["_fp"]] = {"t": now, "pushed": False,
                                  "title": str(n.get("title", ""))[:52] + "[同事件合并]"}
                skipped += 1

    # 5c. 逐代表：LLM未判定挂起 → 强档方向门槛 → 阈值过滤 → 跨轮同事件拦截 → 推送
    pushed_events = state.setdefault("pushed_events", [])
    for n, j in reps:
        if not j.get("judged", True):
            # 2026-08-03 用户口径：全部资讯必须经 LLM 判定。
            # 未判定条目不推、不落指纹 → 下轮重新送 LLM 判定（避免规则误判方向）。
            logger.info(f"LLM 未判定，挂起下轮重试: {n.get('title', '')[:40]}")
            skipped += 1
            continue
        # 2026-08-04 用户口径：仅强利好/强利空（bullish/bearish）推送；
        # 弱档/中性/混合（mildly_bullish/mildly_bearish/neutral/mixed）一律不推，
        # 覆盖 market/sector/stock、外围科技必推、科技防漏推等全部路径。
        if j.get("direction") not in ("bullish", "bearish"):
            logger.info(f"非强档方向({j.get('direction')})，不推: {n.get('title', '')[:40]}")
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}
            skipped += 1
            continue
        # 2026-08-11 修复（审核实证 13/61 滥推）：栏目汇总/指数播报/盘面异动类
        # 即使 LLM 判强档也硬过滤（"晚间新闻精选""隔夜要闻""KOSPI涨超2%""概念异动拉升"），
        # 属"非重大消息"，按用户口径（仅重大事件推送）不应推。记 seen 标注原因。
        noise_reason = _is_noise_push(n, j, leader_watchlist)
        if noise_reason:
            logger.info(f"噪声过滤({noise_reason})，不推: {n.get('title', '')[:40]}")
            seen[n["_fp"]] = {"t": now, "pushed": False,
                              "title": str(n.get("title", ""))[:52] + f"[{noise_reason}不推]"}
            skipped += 1
            continue
        # 判定顺序：
        # 1) LLM 明确判定不重大（push=false）→ 默认一票否决
        # 2) 例外放行：科技板块级资讯/科技龙头个股（_is_domestic_tech 规则兜底，
        #    "科技不能不漏"）或外围科技（_is_overseas_tech）——用户口径：影响科技
        #    板块的资讯必须推，只影响中小市值个股自身的业绩/回购等仍按 LLM 否决
        # 3) 阈值只对 LLM 认为值得推的条目进一步收紧
        # 龙头判定: LLM 标注 或 命中自选龙头名单
        is_leader = bool(j.get("is_leader_stock")) or _hit_watchlist(n, leader_watchlist)
        sectors = j.get("sectors") or []
        scope = str(j.get("scope", "stock") or "stock").lower()
        tech_override = (
            # 板块级科技资讯：LLM 即使判不推也放行（score≥5 或 强方向，避免滥推）
            scope == "sector" and _is_domestic_tech(n, sectors)
            and (_to_float(j.get("score", 0)) >= 5
                 or str(j.get("direction", "neutral")) in ("bullish", "bearish"))
        ) or (
            # 科技龙头个股的重大消息：LLM 判不推时仍放行
            scope == "stock" and is_leader and _is_domestic_tech(n, sectors)
            and (_to_float(j.get("score", 0)) >= 5
                 or str(j.get("direction", "neutral")) in ("bullish", "bearish"))
        )
        if j.get("push") and _passes_threshold(
                mode, j.get("score", 0), j.get("direction", "neutral"),
                j.get("scope", "stock"), leader_stock=is_leader):
            pass_round = True
        elif j.get("push") and _is_overseas_tech(n, sectors):
            # 外围消息且涉及科技板块：即使未达常规阈值也推（用户要求外围科技必推）
            pass_round = True
        elif tech_override:
            # 科技板块级/科技龙头资讯：LLM 判不推也放行（防漏推科技）
            pass_round = True
        else:
            pass_round = False

        if not pass_round:
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}
            skipped += 1
            continue

        # 跨轮同事件拦截：48h 内已推过同事件则不再推（防多源报道时间差导致重复推送）
        if any(_is_same_event(n["_sig"], pe) for pe in pushed_events):
            logger.info(f"同事件48h内已推送，跳过: {n.get('title', '')[:50]}")
            seen[n["_fp"]] = {"t": now, "pushed": False,
                              "title": str(n.get("title", ""))[:52] + "[同事件已推]"}
            skipped += 1
            continue

        # 同题材饱和拦截（2026-08-12 防偏科）：同一板块/实体 24h 内已推达上限
        # （默认 5 条）后不再推——存储行情日 17 推实证；market 级（宏观数据/大盘）
        # 豁免，CPI 等宏观数据永不受限。
        if _topic_saturated(n["_sig"], pushed_events):
            logger.info(f"同题材已饱和(≥{TOPIC_PUSH_LIMIT}条/24h)，不推: {n.get('title', '')[:50]}")
            seen[n["_fp"]] = {"t": now, "pushed": False,
                              "title": str(n.get("title", ""))[:52] + "[同题材已饱和]"}
            skipped += 1
            continue

        content = format_push_alert(n, j)
        if dry_run:
            logger.info(f"[dry-run] 将推送: {n.get('title', '')[:50]}")
            # Windows 控制台默认 GBK 无法打印 emoji，先切 UTF-8 容错
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
            print("\n===== 将推送内容预览 =====\n" + content + "\n==========================")
            seen[n["_fp"]] = {"t": now, "pushed": True, "title": str(n.get("title", ""))[:60]}
            pushed_events.append({**n["_sig"], "t": now})
            pushed += 1
        else:
            # 推送标题直接用新闻原文标题（避免显示"重要资讯"占位符）
            push_title = str(n.get("title", "") or "")[:80] or "重大资讯"
            result = _send_alert_item(push_config, push_title, content)
            if result.get("code") == 200 or result.get("errcode") == 0:
                logger.info(f"推送成功: {n.get('title', '')[:50]}")
                seen[n["_fp"]] = {"t": now, "pushed": True, "title": str(n.get("title", ""))[:60]}
                pushed_events.append({**n["_sig"], "t": now})
                pushed += 1
            else:
                # 推送失败：不记录指纹，下轮重试（避免重大消息丢失）
                logger.error(f"推送失败（下轮重试）: {n.get('title', '')[:50]} | {result}")
                skipped += 1

    # 其余未进入候选的条目也记录指纹（跳过溢出挂起的 pending 条目——它们下轮重试）
    cand_fps = {n["_fp"] for n in candidates}
    for n in new_items:
        if n["_fp"] not in cand_fps and n["_fp"] not in seen and n["_fp"] not in pending:
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}

    # pending 条目本轮已重新进入判定并落 seen（推/不推均已定论）→ 从 pending 移除，
    # 避免陈旧挂起记录残留导致计数混乱（2026-08-06 P1-2）
    for n in candidates:
        if n["_fp"] in seen:
            pending.pop(n["_fp"], None)
    for fp in list(pending.keys()):
        if fp in seen:
            pending.pop(fp, None)

    if not dry_run:
        save_state(state)

    stats = {
        "fetched": len(news_list),
        "new": len(new_items),
        "prefiltered": len(candidates),
        "pushed": pushed,
        "skipped": skipped,
    }
    logger.info(f"本轮完成: 拉取{stats['fetched']} 新增{stats['new']} 预筛{stats['prefiltered']} 推送{stats['pushed']} 未推{stats['skipped']}")

    # 心跳告警（2026-08-06 新增）：有新增资讯但连续多轮 0 推送时，提示可能系统异常
    # （正常行情下新资讯经 LLM 判定后通常有少量推送；长期 0 推送可能是 LLM 端点故障/
    # 阈值过严/数据源异常，人工需确认而非静默接受）
    if stats["new"] > 0 and stats["pushed"] == 0:
        _zero_push_streak[0] += 1
        if _zero_push_streak[0] >= HEARTBEAT_ZERO_PUSH_WARN_ROUNDS:
            logger.warning(
                f"[heartbeat] 已连续 {_zero_push_streak[0]} 轮有新增资讯但 0 推送！"
                f"请检查 LLM 端点/推送后端/阈值配置（fetch={stats['fetched']}, "
                f"new={stats['new']}, prefilter={stats['prefiltered']}）")
    else:
        _zero_push_streak[0] = 0  # 有推送或本轮无新增 → 重置计数
    return stats


def _is_trading_day() -> bool:
    """A股交易日判断（RT_ALWAYS_ON=0 时启用）"""
    try:
        import chinese_calendar  # type: ignore
        from datetime import date
        return chinese_calendar.is_workday(date.today())
    except ImportError:
        return datetime.now(BJT).weekday() < 5


def main():
    parser = argparse.ArgumentParser(description="实时重要资讯推送")
    parser.add_argument("--loop", action="store_true", help="常驻循环模式（本地守护进程）")
    parser.add_argument("--dry-run", action="store_true", help="只诊断不推送不保存状态")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    always_on = os.getenv("RT_ALWAYS_ON", "1").strip() == "1"
    if not always_on and not _is_trading_day():
        logger.info("今日非A股交易日（RT_ALWAYS_ON=0），跳过")
        return

    if args.loop:
        poll_seconds = int(os.getenv("RT_POLL_SECONDS", "120"))
        logger.info(f"实时推送守护进程启动，轮询间隔 {poll_seconds}s（Ctrl+C 退出）")
        if os.getenv("GIST_TOKEN") and os.getenv("GIST_ID"):
            logger.warning("本地轮询与云端 GitHub Actions 共享同一 Gist 状态。"
                           "若云端定时任务也在运行，建议只保留一个运行端"
                           "（状态已做读-改-写合并，但双端同跑仍浪费 LLM 额度且偶发重复）")
        while True:
            try:
                run_once(dry_run=args.dry_run)
            except Exception as e:
                logger.error(f"本轮执行异常: {e}", exc_info=True)
            time.sleep(poll_seconds)
    else:
        run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
