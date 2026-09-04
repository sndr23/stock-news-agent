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
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

BJT = timezone(timedelta(hours=8))

logger = logging.getLogger("real_time_push")


def _state_timestamp(value: str):
    """把状态中持久化的北京时间字符串转换为 epoch 秒。"""
    try:
        parsed = datetime.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=BJT).timestamp()


def _as_list(v):
    """LLM 字段类型防御：字符串→单元素列表，杜绝 set()/join 拆字

    2026-08-13 P1 修复：LLM 返回 sectors/entities/affected_stocks 为字符串
    （非 JSON 数组）时，`set('半导体')` 会拆成 {'半','导','体'}、`' '.join('半导体')`
    变 '半 导 体'——科技词匹配失效（漏推科技）、同事件合并/饱和计数失真。
    统一收敛为：None→[]；str→[v]；list/tuple/set→list；其他→[]。
    """
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple, set, frozenset)):
        return list(v)
    return []


def _env_int(name: str, default: int) -> int:
    """安全读取整数环境变量：非法值回退默认并告警（2026-08-13 P2 修复）

    此前 RT_TOPIC_LIMIT 模块级 int() 无防御，env 非法值 → import 即崩（云端 CI 同挂）。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning(f"环境变量 {name} 无效: {raw!r}，使用默认值 {default}")
        return default

# ============================================================
# 复用项目现有能力（不重复造轮子）
# ============================================================
from src.tools.data_fetchers import get_stock_news, get_market_signals, dedup_news_3layer  # 多源聚合抓取 + 三层近似去重
from src.tools.calculators import (
    calculate_prefilter_importance,   # 预筛评分
    _EVENT_KEYWORD_GROUPS,            # 事件关键词组
    _extract_core_numbers,            # 核心金额提取
    predict_direction_by_rules,       # 规则方向兜底（词表全，含扭亏/退市/涨超/走弱等）
    _has_tech_keyword,                # 科技硬件词匹配（词边界感知，大额经营事件直通用）
)  # 多源事件签名
from src.tools.push import push_via_wecom, push_via_pushplus  # 推送（含重试）
from src.strategy.state_io import atomic_write_json, get_gist_config, patch_gist_file
from src.strategy.data_freshness import _is_workday
# LLM 调用与 JSON 修复（2026-08-06 起从共享模块导入，不再依赖废弃的批处理管线 nodes.py）
from src.llm_client import _call_llm_api, _repair_json
from src.tools.keyword_tables import (                      # 共享关键词表（单一事实来源）
    HIGH_SIGNAL_KEYWORDS,
    OVERSEAS_TECH_KEYWORDS,
    OVERSEAS_SOURCE_MARKERS,
    TECH_SECTOR_WORDS_WIDE,     # 2026-08-19: 国内降级判定宽词表（原本地 _TECH_SECTOR_WORDS 收编）
    has_signal_keyword,        # 2026-08-06: 词边界感知的信号词匹配（ST/IPO 防误命中）
    find_signal_keywords,      # 返回命中的信号词列表（事件指纹 sig 路径用）
    find_signal_fp_keywords,   # 2026-08-07: 指纹专用信号词（排除宽泛市场词，防跨事件指纹合并）
    has_overseas_tech_keyword, # 2026-08-13: 词边界感知的外围科技词匹配（AI 不误命中 DUBAI）
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

# 未推条目的独立窗口（小时，2026-09-01 新增）：见 _expire_seen_fingerprints 说明。
# 已推条目仍需完整 48h（复盘列表 + 48h 同事件拦截依赖）。
STATE_WINDOW_HOURS_UNPUSHED = 24

# 候选溢出挂起重试上限（2026-08-06 新增）：同一指纹连续 N 轮溢出后放弃（写 seen），
# 防止突发行情持续超限时 pending 无限累积 / 无限重试消耗 LLM 额度
MAX_PENDING_RETRY = 3

# pending 序列化字节上限（2026-09-04 P1-1 新增）：挂起条目带全量 payload 后
# 体积上升，200 条 × ~700B ≈ 140KB，叠加 seen(700KB)+pushed/candidate(210KB)
# 需守住 Gist 单文件 1MB 硬限。超限按时间裁剪到 150 条。
PENDING_MAX_BYTES = 200_000

# seen 指纹条数上限（2026-08-13 P0 新增）：48h 清理后仍超上限则按时间保留最新。
# 此前 seen 无上限（峰值 4759 条 → 状态文件 0.72MB），Gist 写入截断损坏是
# "状态被覆盖清空"事故的诱因。
# 2026-09-01 上调 3000 → 12000：实测单日未推条目（pushed=False）达 2972 条，
# 3000 上限一天就被击穿，seen 时间窗口缩到不足 1 天（09-01 04:03~22:33），
# 凌晨已推事件被挤出 seen → 盘后复盘漏条 + 次日重复推送风险。
# 体积按 _prune_seen 的字节上限兜底：Gist 单文件 1MB 硬限（8-13 状态文件
# 膨胀到 0.72MB 时即发生过写入截断损坏），700KB 留足余量。条数上限只是
# 第一道闸，真正决定能不能安全落盘的是字节上限。
SEEN_MAX = 12000
SEEN_MAX_BYTES = 700_000

def _est_seen_bytes(seen: dict, sample: int = 200) -> int:
    """seen 序列化后的精确字节数（全量 json.dumps，与 _gist_save 写入口径一致）。

    2026-09-02 修正：原抽样估算口径（指纹 + t + title + 60B ≈ 150B/条）与
    实际序列化（全部字段 + indent=2 格式化 ≈ 175B/条）不一致，且记录里存
    的是 title_norm 而 title 字段往往不存在——估算系统性偏低。9-02 实证：
    4055 条估算 607KB 未触发 700KB 裁剪，实际 709KB。700KB 级整表 dumps
    每轮一次成本可忽略，改精确计算后 SEEN_MAX_BYTES 才是真硬上限。
    注意：该上限只约束 seen 本身；整文件还含 candidate_events/pending/
    pushed_events（约 +210KB），917KB 实证写入侧仍安全（<1MB Gist 硬限），
    读取侧已由 _gist_load 的 raw_url 回退根治，不再依赖体积控制。
    """
    if not seen:
        return 0
    return len(json.dumps(seen, ensure_ascii=False, indent=2).encode("utf-8"))


def _est_pending_bytes(pending: dict) -> int:
    """pending 序列化后的精确字节数（2026-09-04 P1-1：带 payload 后体积兜底用）"""
    if not pending:
        return 0
    return len(json.dumps(pending, ensure_ascii=False, indent=2).encode("utf-8"))


def _prune_seen(seen: dict, max_items: int = SEEN_MAX,
                max_bytes: int = SEEN_MAX_BYTES) -> dict:
    """指纹表裁剪：条数与字节双上限，超限时优先淘汰未推条目。

    2026-09-01：pushed=True 的条目是盘后复盘列表与 48h 同事件拦截的依据，
    pushed=False 只是"本轮已处理、别再判"的标记，去重价值低。单纯按时间
    裁剪会把当天已推事件挤出去——9-01 实锤：00:32 推送的「君正股份 DRAM
    涨价」被挤出 seen，复盘"今日已推事件"直接漏掉这一条。
    排序键 (pushed, t) 使 False 组整体排在前面，组内按时间升序，
    于是切片淘汰的是"最老的未推条目"。
    字节上限优先于条数上限：Gist 单文件 1MB 是硬限，超限会写入截断损坏
    （8-13 事故根因），条数再多也必须让位于体积安全。
    """
    if not seen:
        return seen
    per = max(_est_seen_bytes(seen) / len(seen), 1.0)
    allowed = min(max_items, max(1, int(max_bytes / per)))
    if len(seen) <= allowed:
        return seen
    items = sorted(seen.items(),
                   key=lambda kv: (bool(kv[1].get("pushed")), str(kv[1].get("t", ""))))
    return dict(items[len(items) - allowed:])


# 心跳告警（2026-08-06 新增）：有新增资讯但连续 N 轮 0 推送时输出告警日志，
# 帮助区分"确实没大事" vs "系统静默故障"
HEARTBEAT_ZERO_PUSH_WARN_ROUNDS = 6
# 进程内计数器（本地 --loop 常驻进程跨轮累计；云端单轮运行无影响）
_zero_push_streak = [0]
# watchlist 空名单告警去重（2026-08-13 P1-2：--loop 模式只告警一次，避免每轮刷屏）
_watchlist_warned = [False]

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
    return has_overseas_tech_keyword(text)


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
    return has_overseas_tech_keyword(text)


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
    stocks = set(_as_list(news.get("affected_stocks")))
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
    title_norm = _normalize_title(news.get('title', ''))
    # 标题主语片段（2026-08-13 修复：news 类无 affected_stocks/name 字段，st 恒为空，
    # 导致"寒武纪回购5亿"与"宁德时代回购5亿"事件组+金额相同 → 指纹碰撞漏推。
    # 掺入标题前 6 字区分公司主语（多源同事件报道的主语通常一致，不影响合并）。
    title_head = title_norm[:6]
    if hit_signal:
        # 命中高信号词（宏观/监管级）：以信号词+事件组+个股为指纹，不掺入数字——
        # 数字表达不稳定（"1700亿" vs "一千七百亿"），且同信号词下数字分叉
        # 会把同一事件的多源报道拆成不同指纹
        if stocks:
            key = f"{date}|sig:{sorted(hit_signal)}|ev:{sorted(events)}|st:{sorted(stocks)}"
        else:
            key = f"{date}|sig:{sorted(hit_signal)}|ev:{sorted(events)}|t:{title_head}"
    elif stocks or events or numbers:
        # 公告/公司事件：个股+事件组+核心金额辅助区分（"XX:回购5亿" vs "XX:回购8亿"）
        if stocks:
            key = f"{date}|st:{sorted(stocks)}|ev:{sorted(events)}|num:{sorted(numbers)}"
        else:
            key = f"{date}|ev:{sorted(events)}|num:{sorted(numbers)}|t:{title_head}"
    else:
        # 普通流水新闻：归一化标题指纹
        key = f"{date}|t:{title_norm}"
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


# 实体别名归一化表（2026-08-13 修复：LLM 对同一实体的表述不稳定——"大摩" vs "摩根士丹利"、
# "三星电子" vs "三星"、英文 vs 中文，导致跨源同事件合并失效、重复推送）。
# 仅收"无歧义"的别名（简称→规范全称/中英对照），不收录有歧义的简称。
_ENTITY_ALIAS = {
    "大摩": "摩根士丹利", "小摩": "摩根大通",
    "TSMC": "台积电", "三星电子": "三星", "Samsung": "三星",
    "海力士": "SK海力士", "SK Hynix": "SK海力士",
    "NVIDIA": "英伟达", "Tesla": "特斯拉", "Apple": "苹果",
    "Google": "谷歌", "Alphabet": "谷歌", "Microsoft": "微软",
    "Amazon": "亚马逊", "欧洲央行": "欧央行", "ECB": "欧央行",
    "日央行": "日本央行", "BOJ": "日本央行", "Fed": "美联储",
    "上海市": "上海", "北京市": "北京",
}


def _normalize_entity(e: str) -> str:
    """实体归一化：剥离股票代码后缀（台积电(TSM.N)→台积电）+ 别名映射"""
    if not e:
        return e
    e = str(e).strip()
    # 剥离末尾股票代码后缀 "(TSM.N)" / "(AAPL.O)" / "(00700.HK)"
    e = re.sub(r"[（(][A-Za-z0-9.\-]+[)）]$", "", e).strip()
    return _ENTITY_ALIAS.get(e, e)


def _sectors_overlap(sec_a, sec_b) -> set:
    """板块交集（子串包含语义，2026-08-13 修复）

    LLM 抽的 sectors 不稳定："AI算力" vs "AI"+"算力"、"算力/CPO" vs "CPO"，
    精确相等导致同板块事件漏合并（上海算力补贴×2 实证）。用子串包含判断同板块。
    """
    a = {str(s).strip() for s in _as_list(sec_a) if str(s).strip()}
    b = {str(s).strip() for s in _as_list(sec_b) if str(s).strip()}
    hit = set()
    for sa in a:
        for sb in b:
            if sa and sb and (sa == sb or sa in sb or sb in sa):
                hit.add(sa if len(sa) <= len(sb) else sb)
    return hit


def _push_event_sig(news: dict, judge: dict) -> dict:
    """生成推送级事件签名：规则抽取(个股/事件组/金额) + LLM主体(entities) + 归一化标题

    指纹 _news_fingerprint 解决"完全同一条"的跨轮去重；本签名解决"同一事件的
    不同报道"（标题措辞/金额表述/信号词子集不同导致指纹分裂，
    恩智浦收购Ambarella三源三推实证）。
    """
    stocks, events, numbers = _event_signature_light(news)
    entities = {_normalize_entity(str(e).strip()) for e in _as_list(judge.get("entities")) if str(e).strip()}
    sectors = {str(s).strip() for s in _as_list(judge.get("sectors")) if str(s).strip()}
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
        "stocks": sorted(set(_as_list(sig_a.get("stocks"))) | set(_as_list(sig_b.get("stocks")))),
        "entities": sorted(set(_as_list(sig_a.get("entities"))) | set(_as_list(sig_b.get("entities")))),
        "events": sorted(set(_as_list(sig_a.get("events"))) | set(_as_list(sig_b.get("events")))),
        "numbers": sorted(set(_as_list(sig_a.get("numbers"))) | set(_as_list(sig_b.get("numbers")))),
        "sectors": sorted(set(_as_list(sig_a.get("sectors"))) | set(_as_list(sig_b.get("sectors")))),
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
    # 2026-09-04 审核实证：金十"期货热点追踪""局势跟踪"为日报式栏目（中东/俄乌各推一条的来源），
    # 属栏目汇总类噪声。补入；剥离栏目词后仍含重大事件（如"局势跟踪：以色列空袭核设施"）由
    # 分支1 的"栏目内嵌重大事件"语义放行。
    "期货热点追踪", "局势跟踪",
]
# 指数盘中行情播报的波动措辞（与 _INDEX_TOKENS 组合判定，避免误伤个股消息）
_NOISE_INDEX_MOVE = [
    "涨超", "跌超", "涨逾", "跌逾", "涨幅扩大", "跌幅扩大", "涨幅扩大至",
    "跌幅扩大至", "现报", "开涨", "开跌", "收涨", "收跌", "上涨", "下跌",
    # 2026-08-25 审核实证："午评创业板指半日跌35"（"半日跌"）——既有
    # MOVE 词表缺"半日/盘中/日内涨跌"表述，"跌35"非"下跌"漏判。补入。
    "半日跌", "半日涨", "盘中跌", "盘中涨", "日内跌", "日内涨",
    # 2026-09-04 审核实证："日元涨幅超过1"——"涨幅超过/跌幅超过"表述不在词表漏网。补入。
    "涨幅超过", "跌幅超过", "涨幅超", "跌幅超",
    # 2026-09-04 审核实证："柴油期货价格创新高"——"创新高/创新低"为行情措辞漏网。
    # 仅与 _NOISE_INDEX_NAMES 组合判定，个股"中际旭创创新高"不命中指数名单不受影响。
    "创新高", "创新低",
]
# 纯盘面异动措辞（板块/概念异动、非龙头个股异动；龙头个股消息由 is_leader 例外放行）
_NOISE_INTRADAY_MARKERS = [
    "异动拉升", "直线涨停", "直线跌停", "盘初走强", "盘中走强", "短线走高",
    "震荡拉升", "尾盘拉升", "午后拉升", "冲高回落", "快速走强", "集体走强",
    # 2026-08-13 审核实证漏网："PCB概念表现活跃 方邦股份涨超10%" 被推送——
    # "表现活跃/概念活跃"等盘面情绪措辞不在词表。补入；龙头例外仍由 is_leader 放行
    # （"中际旭创表现活跃"类不误伤）。
    "表现活跃", "概念活跃", "持续活跃", "反复活跃", "概念走强", "概念走高",
    "集体大涨", "涨停潮", "活跃拉升",
    # 2026-08-25 审核实证：净流出/震荡回升/跳水/涨停等盘面措辞漏网
    # （"主力资金转融券标的板块净流出超742亿"、"人形机器人概念震荡回升兆威机电
    # 涨停"全被推送）。龙头例外仍由 is_leader 放行，不误伤自选龙头（"中际旭创跳水"仍推）。
    "震荡回升", "震荡回落", "震荡走高", "震荡走低",
    "净流入", "净流出", "中单净流出", "资金出逃",
    "快速跳水", "大幅跳水", "跳水", "异动",
    "涨停", "跌停",
]
# 研报/观点/主题类措辞（2026-08-13 P0 修复：tech_override 排除守卫用——
# 命中此类措辞的科技消息属"无具体事件的定性判断"，即使 LLM 判 push=false
# 也**不得**被科技兜底强制放行，让 LLM 的否决生效。实测："机构称物理AI市场空间"
# "液冷投资向零部件纵深传导" 均命中，此前被 tech_override 放行误推。）
_TECH_OVERRIDE_VIEW_WORDS = [
    "机构称", "机构认为", "研报", "点评", "观点", "看好", "展望", "解读",
    "市场空间", "有望", "传导", "开启", "浪潮", "风口", "逻辑", "下注方向",
    # 2026-08-13 晚间复审残余修复：分析师警告/预测措辞 + 无事件佐证的泛板块措辞
    # 实测"摩根士丹利警告供应瓶颈""大摩示警算力不足""多家上市公司加码液冷散热业务"
    # 均命中此前漏网，tech_override 仍强制放行（误推复现）。补词后 LLM 的 push=false 生效。
    "警告", "示警", "预计", "目标价", "评级", "加码",
]
# 指数行情播报需额外覆盖的指数名（_INDEX_TOKENS 之外的出现形式）
_NOISE_INDEX_NAMES = _INDEX_TOKENS + ["KOSPI", "恒生指数", "综合指数"] + [
    # 2026-09-04 审核实证：外盘行情播报漏网——"美元指数3日下跌""美元兑日元跌幅扩大至2"
    # "日元涨幅超过1""WTI原油期货收涨0.79美元报91.01" 全被推送。补外盘行情基准词，
    # 与 _NOISE_INDEX_MOVE 组合判定（仅"基准名+涨跌措辞"同时命中才拦，不误伤事件性新闻）。
    # "柴油"用"柴油期货/柴油价格"避免误伤"柴油车/柴油发电机"类标题。
    "美元指数", "美元兑日元", "日元", "WTI", "原油期货", "柴油期货", "柴油价格",
]

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

# 宏观数据发布后的市场反应合并词（2026-08-12 实证：21:31"美国CPI符合预期美股高开"
# vs 22:01"美国7月通胀表现温和美股盘初纳指涨09光通信存储普涨"——同一宏观事件
# 48h 内推两次。entities 各抽各的（SK海力士/美光 vs CoreWeave/超微电脑）无交集，
# 标题共享段短，既有 7 个合并分支全部兜不住。守卫：双方均含宏观数据词 +
# 同市场域（防"德国CPI vs 美国CPI"误并——德国CPI标题无美股域词）+ 方向一致。）
_MACRO_EVENT_WORDS = ["CPI", "PPI", "通胀", "物价", "非农", "失业率", "GDP",
                      "利率决议", "FOMC", "就业数据"]

# 市场状态类事件组（2026-08-14 修复：日本央行加息三连推实证）
# "行情下跌"由 _EVENT_KEYWORD_GROUPS 规则命中 content 中"走低/暴跌/下挫"等词提取，
# 属市场状态描述而非事件动词。它使 _is_same_event 的"共享事件组"与
# "双方均无事件组"分支同时失效——第一条 events=["行情下跌"] 与其余 events=[] 的
# 报道无法匹配（shared_ev=∅ 且 not ev_a 不成立），同一事件多源报道 48h 内重复推送。
# _is_same_event 计算事件组前统一过滤，避免污染事件语义。
_MARKET_STATE_EVENTS = {"行情下跌", "行情上涨", "行情震荡"}

# 央行/货币当局实体（归一化后名称，2026-08-14 修复：宏观流动性事件跨源合并兜底）
# 日本央行加息类宏观事件：多源快讯措辞差异大（"最快可能在9月加息" vs
# "加息或提速美元对日元急跌…流动性冲击"），LCS 仅 4 字、jaccard 0.15~0.20、
# 无事件组/板块交集，既有全部合并分支兜不住（"加息"不在事件锚表、
# _MACRO_EVENT_WORDS 无"加息"、日股无市场域词）。新增分支守卫：
# 双方共享同一央行实体 + 标题均含宏观政策词 + 政策方向不冲突 → 同事件。
_CENTRAL_BANK_ENTITIES = {
    "日本央行", "美联储", "欧央行", "中国人民银行", "英国央行",
    "韩国央行", "澳洲联储", "瑞士央行", "加拿大央行",
}
# 宏观政策鹰派/鸽派方向词（方向守卫，防"考虑加息 vs 考虑降息"误并——
# _title_direction_conflict 的涨跌表不含"加息/降息"：加息无"涨/升"字）
_POLICY_HAWK_WORDS = ("加息", "升息", "缩表", "紧缩", "收紧")
_POLICY_DOVE_WORDS = ("降息", "扩表", "宽松", "降准", "量化宽松", "放水")
# 宏观政策事件触发词（合并判定：标题描述同一央行政策动作；需配合央行实体守卫）
# 仅含方向性政策动作（购债无方向——"购债操作安排"中性日常 vs "削减购债"鹰派，
# 不宜作为合并触发，避免"购债安排"与"加息传闻"误并）
_MACRO_POLICY_EVENT_WORDS = (
    "加息", "降息", "缩表", "扩表", "宽松", "紧缩",
    "升息", "降准", "量化宽松", "YCC", "收益率曲线控制",
)

# 行情普涨/普跌类措辞（sectors 交集合并用，2026-08-12 实证：22:01 同轮双推
# "美股光通信存储概念股普涨诺基亚升逾9" vs "美国7月通胀表现温和美股盘初纳指涨09
# 光通信股存储芯片股普涨"——entities 各抽各的（诺基亚/Ciena/Coherent vs
# CoreWeave/超微电脑），无共享时段词/指数词，LCS 仅"光通信"3 字兜不住。
# 两标题共享板块（光通信/半导体/存储）且均为美股行情 → 合并。
# 不含单字"涨/跌"防"涨价/跌幅"等非行情语境误并。）
_MARKET_MOVE_WORDS = [
    "普涨", "普跌", "高开", "低开", "走强", "走弱", "上涨", "下跌",
    "涨超", "跌超", "升逾", "涨逾", "跳水", "回升", "大涨", "大跌",
    "拉升", "上扬", "下挫", "冲高", "回落", "涨近", "跌近",
    "涨幅扩大", "跌幅扩大",
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
    1. 完全相等（先经 _normalize_entity 归一化别名/代码后缀）
    2. 互相包含（"索尼" ⊂ "索尼半导体解决方案公司"）
    3. 共享连续子串占较短实体 ≥60% 且 ≥2 字（"索尼半导体" vs "索尼"）
    """
    na = {_normalize_entity(ea) for ea in ent_a if ea}
    nb = {_normalize_entity(eb) for eb in ent_b if eb}
    for ea in na:
        for eb in nb:
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


def _is_index_move_text(text: str) -> bool:
    """是否"指数名+行情措辞"（指数盘中播报，与 _is_noise_push 分支2 同判据）"""
    return (any(i in text for i in _NOISE_INDEX_NAMES)
            and any(m in text for m in _NOISE_INDEX_MOVE))


def _is_intraday_move_text(text: str) -> bool:
    """是否命中盘面异动措辞（与 _is_noise_push 分支3 同判据）"""
    return any(m in text for m in _NOISE_INTRADAY_MARKERS)


def _is_noise_push(news: dict, judge: dict, leader_watchlist: set) -> str:
    """栏目汇总/指数播报/盘面异动类噪声识别（2026-08-11 修复）

    返回噪声原因字符串（非空=应过滤不推），空串=正常条目。
    判定顺序：
    1. 栏目汇总类（新闻精选/要闻速递/九点特供/风口研报/午评/收评等）→ 不推；
       但剥离栏目词后仍含高信号词/宏观数据（栏目内嵌重大事件）→ 放行
    2. 指数盘中行情播报（指数名 + 涨跌幅度措辞）→ 不推
    3. 板块/概念盘面异动（异动拉升/直线涨停等）→ 非龙头不推；
       LLM 龙头标记或命中自选名单（如"中际旭创跌超3%"）保留
    """
    title = str(news.get("title", "") or "")
    if not title:
        return ""
    if any(m in title for m in _NOISE_COLUMN_MARKERS):
        # 2026-08-13 修复（漏推实证）：栏目词仅是栏目前缀/栏目名，真正价值在栏目词之外的内容。
        # 此前"只要标题含栏目词即一票否决"，会把"晚间新闻精选：央行宣布降准"、
        # "涨停分析：中际旭创获50亿大单涨停"等栏目内嵌的重大事件误杀。
        # 现剥离全部命中的栏目词后，若剩余仍含高信号词或宏观数据发布，
        # 判定为"栏目内嵌重大事件"放行（后续仍走强档门槛 + LLM push 判定把关）。
        rest = title
        for m in _NOISE_COLUMN_MARKERS:
            if m in rest:
                rest = rest.replace(m, "")
        # 2026-08-25 审核实证："午评创业板指半日跌35…算力硬件"剥离"午评"后
        # 因 has_signal_keyword（算力硬件）被误放行，实为指数+盘面行情播报。
        # 剥离后若仍是"指数名+行情措辞"或"盘面异动措辞"→ 栏目/行情不推。
        # 但"栏目内嵌重大事件"仍放行（2026-08-13 语义）：CPI 数据发布、龙头
        # 大额经营事件不得因含"跳水/涨停"盘面词被误杀（test_fix_20260813 回归）。
        if has_signal_keyword(rest) or _is_macro_data_release(rest):
            if not (_is_macro_data_release(rest) or _is_major_deal_event(rest)
                    or _hit_headline_entity(rest)):
                if _is_index_move_text(rest) or _is_intraday_move_text(rest):
                    return "栏目汇总"
            return ""
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
TOPIC_PUSH_LIMIT = _env_int("RT_TOPIC_LIMIT", 5)
TOPIC_PUSH_WINDOW_H = 24.0

# 2026-08-25 审核实证：market 级"sectors/entities 全豁免"被人利用——美加关税×7/伊朗×4
# 全部 scope=market 无限豁免溢出剩余。仅"宏观数据发布类"（CPI/非农/利率决议）保持豁免；
# 地缘/制裁/关税/央行表态类 market 参与"同主题饱和"，上限 3 条（比板块默认 5 条更严）。
_MARKET_THEME_PATTERNS = [
    ("美加关税", ["加拿大", "关税", "卢特尼克", "美加"]),
    ("伊朗制裁", ["伊朗", "里亚尔", "霍尔木兹"]),
    ("日本央行", ["日本央行", "日元", "植田"]),
    ("美债/美财长", ["美债", "贝森特"]),
    # 2026-09-04 审核实证：中东/油价系 48h 推约 8 条（特朗普打击伊朗、局势跟踪×2、
    # 驻军延长、互放狠话、柴油新高×2、油价周涨幅）——既有 4 槽位无俄乌/油价/以黎，
    # "中东驻军"不含"伊朗"关键词直接逃逸。补两槽位；"柴油"用"柴油期货/柴油价格"
    # 避免"柴油车/柴油发电机"误入油价主题。霍尔木兹保留在"伊朗制裁"槽避免重复计数。
    ("俄乌冲突", ["俄乌", "乌克兰", "克里姆林宫"]),
    ("中东局势/油价", ["中东", "以色列", "黎巴嫩", "油价", "原油", "柴油期货", "柴油价格"]),
]
MARKET_TOPIC_PUSH_LIMIT = _env_int("RT_MARKET_TOPIC_LIMIT", 3)


def _market_theme_keys(text: str) -> set:
    """标题命中的 market 主题键集合（market 级同主题饱和计数槽位）"""
    return {tk for tk, kws in _MARKET_THEME_PATTERNS if any(k in text for k in kws)}


def _sig_theme_keys(sig: dict) -> set:
    """事件命中的 market 主题键：标题 + 实体联合扫描。

    2026-08-25 实证漏拦："国际海事组织…中东海域68起袭击"标题无主题词，
    但 entities=[伊朗]——主题归属承载在实体上，仅扫标题会漏。
    """
    text = str(sig.get("title_norm") or "")
    for k in ("entities", "stocks"):
        text += " " + " ".join(str(x) for x in _as_list(sig.get(k)))
    return _market_theme_keys(text)


def _market_theme_saturated(keys: set, pushed_events: list) -> bool:
    """同 theme 24h 内已推 ≥ MARKET_TOPIC_PUSH_LIMIT 条则饱和（与板块饱和同窗口口径）"""
    now_ts = time.time()
    cnt = 0
    for pe in pushed_events:
        t = str(pe.get("t") or "")
        ts = _state_timestamp(t)
        if ts is None:
            continue
        if now_ts - ts > TOPIC_PUSH_WINDOW_H * 3600:
            continue
        if _sig_theme_keys(pe) & keys:
            cnt += 1
    return cnt >= MARKET_TOPIC_PUSH_LIMIT


def _is_macro_policy(news: dict) -> bool:
    """候选是否属宏观/政策/地缘类（溢出排序优先，防宏观被科技噪声挤占）"""
    title = str(news.get("title", "") or "")
    content = str(news.get("content", "") or "")
    text = f"{title} {content}"
    return any(k in text for k in _MACRO_POLICY_KEYWORDS)


def _topic_saturated(sig: dict, pushed_events: list) -> bool:
    """同题材推送是否已达饱和：market 级仅宏观数据豁免（其余走主题槽位，上限3）；
    板块/实体 24h 内已推 ≥ 上限（5）

    2026-08-13 P1 修复（口径对齐）：板块重叠改用 _sectors_overlap（子串包含，
    "AI算力"与"AI/算力"同板块）、实体重叠用归一化后交集（"大摩"与"摩根士丹利"
    同实体）——此前用精确交集，与 _is_same_event 合并口径不一致，实体为空时
    板块表述不一致会漏判同题材，饱和限流失准。
    """
    scope = str(sig.get("scope") or "stock")
    title = str(sig.get("title_norm") or "")
    # 2026-08-25 修复：market 级仅"宏观数据发布"豁免（CPI/非农等数据本体必须推）；
    # 地缘/制裁/关税/央行表态类 market 走同主题槽位（上限3）。主题检查先于实体
    # 槽位路径——实证漏拦：伊朗海事事件 entities=[伊朗]非空时走实体槽位（上限5，
    # 窗口内仅4条）绕过了主题路径（上限3，窗口内5条本应拦截）。
    if scope == "market":
        if _is_macro_data_release(title):
            return False
        keys = _sig_theme_keys(sig)
        if keys and _market_theme_saturated(keys, pushed_events):
            return True
    secs = set(_as_list(sig.get("sectors")))
    ents = {_normalize_entity(e) for e in _as_list(sig.get("stocks"))} | \
           {_normalize_entity(e) for e in _as_list(sig.get("entities"))}
    if not secs and not ents:
        return False
    now_ts = time.time()
    cnt = 0
    for pe in pushed_events:
        t = str(pe.get("t") or "")
        ts = _state_timestamp(t)
        if ts is None:
            continue
        if now_ts - ts > TOPIC_PUSH_WINDOW_H * 3600:
            continue
        psecs = set(_as_list(pe.get("sectors")))
        pents = {_normalize_entity(e) for e in _as_list(pe.get("stocks"))} | \
                {_normalize_entity(e) for e in _as_list(pe.get("entities"))}
        if _sectors_overlap(secs, psecs) or (ents & pents):
            cnt += 1
    return cnt >= TOPIC_PUSH_LIMIT


def _central_bank_shared(ent_a: set, ent_b: set) -> bool:
    """双方实体是否共享同一央行（归一化后精确匹配，防日本央行 vs 美联储误并）"""
    ca = {_normalize_entity(e) for e in ent_a} & _CENTRAL_BANK_ENTITIES
    cb = {_normalize_entity(e) for e in ent_b} & _CENTRAL_BANK_ENTITIES
    return bool(ca & cb)


def _policy_direction_conflict(ta: str, tb: str) -> bool:
    """宏观政策方向冲突（鹰派 vs 鸽派），防"考虑加息 vs 考虑降息"被宏观分支误并

    _title_direction_conflict 的涨跌表不含"加息/降息"（"加息"无"涨/升"字、
    "降息"含"降"但不含"跌"），对货币政策方向对立识别失效，需单独守卫。
    """
    hawk_a = any(w in ta for w in _POLICY_HAWK_WORDS)
    dove_b = any(w in tb for w in _POLICY_DOVE_WORDS)
    dove_a = any(w in ta for w in _POLICY_DOVE_WORDS)
    hawk_b = any(w in tb for w in _POLICY_HAWK_WORDS)
    return (hawk_a and dove_b) or (dove_a and hawk_b)


class _SameEventCtx(NamedTuple):
    """_is_same_event 规则族共享上下文：签名字段一次性抽取，规则函数零重复解析"""
    ta: str            # 归一化标题（str(x or "")，None→""）
    tb: str
    ent_a: set         # 原始实体集（个股∪实体；归一化/模糊匹配在 _entity_overlap 内做）
    ent_b: set
    ev_a: set          # 事件组（已剔除市场状态类词）
    ev_b: set
    num_a: set         # 核心金额
    num_b: set
    sec_a: set
    sec_b: set
    shared_ev: set     # ev_a & ev_b
    ent_overlap: bool


def _same_event_ctx(sig_a: dict, sig_b: dict) -> _SameEventCtx:
    """从两个推送级签名抽取规则判定所需的全量上下文"""
    ent_a = set(_as_list(sig_a.get("stocks"))) | set(_as_list(sig_a.get("entities")))
    ent_b = set(_as_list(sig_b.get("stocks"))) | set(_as_list(sig_b.get("entities")))
    # 2026-08-14 修复：过滤市场状态类事件组（"行情下跌"由 content 关键词规则提取，
    # 属市场状态描述而非事件动词）。否则非空 events 使"共享事件组"分支
    # （shared_ev=∅）与"双方均无事件组"分支（not ev_a 不成立）同时失效，
    # 同事件多源报道无法合并（日本央行加息三连推实证）。
    ev_a = {e for e in set(_as_list(sig_a.get("events"))) if e not in _MARKET_STATE_EVENTS}
    ev_b = {e for e in set(_as_list(sig_b.get("events"))) if e not in _MARKET_STATE_EVENTS}
    # 2026-08-11 修复：实体模糊重叠（"索尼"⊂"索尼半导体解决方案公司"），
    # 此前仅精确相等，跨源同事件（索尼×台积电合资）48h 内三推实证
    return _SameEventCtx(
        ta=str(sig_a.get("title_norm", "") or ""),
        tb=str(sig_b.get("title_norm", "") or ""),
        ent_a=ent_a, ent_b=ent_b,
        ev_a=ev_a, ev_b=ev_b,
        num_a=set(_as_list(sig_a.get("numbers"))),
        num_b=set(_as_list(sig_b.get("numbers"))),
        sec_a=set(_as_list(sig_a.get("sectors"))),
        sec_b=set(_as_list(sig_b.get("sectors"))),
        shared_ev=ev_a & ev_b,
        ent_overlap=_entity_overlap(ent_a, ent_b),
    )


def _same_event_shared_group(ctx: _SameEventCtx) -> bool:
    """规则1-3：双方共享事件组（shared_ev≠∅）时的合并判定

    1. 主体(实体)交集非空 + 标题守卫 → 同主体同事件
    2. 核心金额交集非空 → 同事件不同措辞同金额
    3. 主体交集为空但标题 LCS≥5 → 多源同事件报道
    """
    if ctx.ent_overlap:
        # 2026-08-11 修复（误并实证）：共享事件组 + 实体模糊重叠 直接合并会把
        # "期货早报…连续21月增持黄金"（事件组=增持）与"多重稳市信号释放…"
        # （content 含"增持"事件组）误并为同事件——两者标题零共享。
        # 守卫：标题需共享 ≥3 字连续子串、字符集 Jaccard ≥0.15、或共享高置信
        # 事件短语锚（寒武纪股权激励类：同个股+同事件组+标题同主题 仍合并）。
        if ctx.ta and ctx.tb:
            return (
                _lcs_len(ctx.ta, ctx.tb) >= 3
                or len(set(ctx.ta) & set(ctx.tb)) / len(set(ctx.ta) | set(ctx.tb)) >= 0.15
                or any(p in ctx.ta and p in ctx.tb for p in _EVENT_PHRASE_ANCHORS)
            )
        return bool(ctx.ta or ctx.tb)
    if ctx.num_a & ctx.num_b:
        return True
    return _lcs_len(ctx.ta, ctx.tb) >= 5


def _same_event_cross_family(ctx: _SameEventCtx) -> bool:
    """事件组抽取不稳定兜底（2026-09-01 修复：美债收益率 4.75% 双推实证）

    21:34"美债10Y收益率升至4.75%为2025年1月以来最高"(entities=美联储)
    vs 22:02"美债10Y收益率突破4.75%创2025年1月以来新高"(entities=美债)
    28 分钟内双推。根因：LLM 对行情类快讯的事件组抽取不稳定——一方抽到
    事件组、另一方为空（或双方各抽各的），shared_ev=∅ 且不满足
    "双方均无事件组"，_is_same_event 直接 return False，连标题
    Jaccard 0.64 的规则4 都没机会执行。

    兜底判据（严守误并风险）：
    - 方向对立终止（"升至" vs "跌破"，沿用 _title_direction_conflict）
    - 金额守卫：双方金额均明确且无交集 → 不同事件
    - 标题高度相似才合并：字符集 Jaccard≥0.55，或连续 LCS≥8 且覆盖
      短标题 ≥50%（美债案例 Jaccard 0.643、LCS"美国10年期国债收益率"=10 字）
    """
    ta, tb = ctx.ta, ctx.tb
    if not (ta and tb):
        return False
    if _title_direction_conflict(ta, tb):
        return False
    if (ctx.num_a and ctx.num_b) and not (ctx.num_a & ctx.num_b):
        return False
    if len(set(ta) & set(tb)) / len(set(ta) | set(tb)) >= 0.55:
        return True
    shorter = min(len(ta), len(tb))
    if shorter >= 8:
        l = _lcs_len(ta, tb)
        if l >= 8 and l / shorter >= 0.5:
            return True
    return False


def _is_same_event(sig_a: dict, sig_b: dict) -> bool:
    """判断两个推送级事件签名是否指向同一事件（满足其一即同事件）

    结构（SNA-04 重构，行为与重构前完全等价，57 处去重专项测试锁定）：
    - 共享事件组族 _same_event_shared_group：规则1-3
    - 无事件组族 _same_event_no_group：规则4-8（市场域/实体锚定/标题相似度）
    - 跨族兜底 _same_event_cross_family（2026-09-01）：事件组单边缺失/双方
      各抽各的时的标题相似度兜底（美债 4.75% 双推实证）

    规则目录：
    1. 主体(个股/LLM实体)交集非空 且 事件组交集非空 → 同主体同事件
       （"寒武纪股权激励大消息" vs "寒武纪:2026年限制性股票激励计划(草案)"）
    2. 核心金额交集非空 且 事件组交集非空 → 同事件不同措辞同金额
       （"30.53亿补充协议" vs "30.53亿元协议"）
    3. 主体交集为空 但事件组交集非空 且 归一化标题最长公共子串≥5 →
       多源同事件报道（"恩智浦洽谈收购Ambarella" vs "安霸股价因传恩智浦洽谈收购而飙升"）
    4. 双方均无事件组（普通流水）且标题字符集 Jaccard≥0.6 → 同一条目的改写
    5. 双方均无事件组 且 归一化标题 LCS 覆盖较短标题≥55%（同实体/同金额放宽至35%）→ 同事件
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
    （板块行情/宏观数据/央行政策/实体锚定等增量规则见各规则函数内注释）
    """
    ctx = _same_event_ctx(sig_a, sig_b)
    if ctx.shared_ev:
        return _same_event_shared_group(ctx)
    if not ctx.ev_a and not ctx.ev_b:
        return _same_event_no_group(ctx)
    # 2026-09-01：事件组单边缺失/双方各抽各的（LLM 对行情类快讯抽取不稳定）
    # 不再直接 return False——美债 4.75% 双推实证，改走跨族相似度兜底
    # （方向/金额守卫 + 高相似阈值，见 _same_event_cross_family）
    return _same_event_cross_family(ctx)


def _same_event_no_group(ctx: _SameEventCtx) -> bool:
    """规则4-8：双方均无事件组（普通流水/板块资讯/行情快讯）时的合并判定"""
    ta, tb = ctx.ta, ctx.tb
    if not (ta and tb):
        return False
    if _title_direction_conflict(ta, tb):
        return False
    r = _same_event_market_rules(ctx)
    if r is not None:
        return r
    # 规则4：标题字符集 Jaccard≥0.6 → 同一条目的改写
    if len(set(ta) & set(tb)) / len(set(ta) | set(tb)) >= 0.6:
        return True
    r = _same_event_entity_anchor_rules(ctx)
    if r is not None:
        return r
    return _same_event_title_similarity(ctx)


def _same_event_market_rules(ctx: _SameEventCtx) -> bool | None:
    """市场域规则族：时段组/指数词/板块行情/宏观数据/央行政策类多源快讯合并

    返回 True=同事件 / False=方向对立终止（不再走后续规则）/ None=无判定。
    """
    ta, tb = ctx.ta, ctx.tb
    # 市场开收盘/复盘类快讯：同时段组 + 同市场域 → 同事件（多源措辞差异大）
    if _session_group(ta, tb) is not None and _market_domain_overlap(ta, tb):
        return True
    # 盘中行情动态（涨超/现报/涨幅扩大等）：同市场域 + 共享市场指数词
    # + 时段不冲突（防 午评 vs 收盘 因共享指数词误并）→ 同事件
    if (not _session_conflict(ta, tb) and _market_domain_overlap(ta, tb)
            and _shared_index_token(ta, tb)):
        return True
    # 2026-08-12 修复（22:01 同轮双推实证）：美股盘初"光通信/存储普涨"
    # 多源报道 entities 各抽各的、无共享时段/指数词、LCS 仅"光通信"3字——
    # 既有分支全部兜不住。新增 sectors 交集合并：均无事件组 + 板块交集
    # + 同市场域 + 任一含行情措辞（方向一致已在入口守卫）。
    # 误并评估：sectors 不同（涨价逻辑 vs 行情情绪）通常交集为空；
    # 交集非空且同域时，少推一条比重复推送更符合用户口径。
    if (ctx.sec_a & ctx.sec_b) and _market_domain_overlap(ta, tb):
        if any(w in ta for w in _MARKET_MOVE_WORDS) or any(w in tb for w in _MARKET_MOVE_WORDS):
            return True
    # 2026-08-12 修复（21:31 vs 22:01 跨轮重复实证）：同一宏观数据事件
    # （CPI符合预期美股高开 / 通胀温和美股盘初纳指涨）48h 内推两次。
    # 双方均含宏观数据词 + 同市场域 → 同事件（方向一致已在入口守卫）。
    # 市场域守卫防不同国家 CPI 误并（德国/意大利 CPI 标题无美股域词）。
    if _market_domain_overlap(ta, tb) and \
            any(w in ta for w in _MACRO_EVENT_WORDS) and \
            any(w in tb for w in _MACRO_EVENT_WORDS):
        return True
    # 2026-08-14 修复（日本央行加息三连推实证）：宏观流动性/货币政策事件
    # 多源报道措辞差异大（"最快可能在9月加息" vs "加息或提速美元对日元急跌
    # …流动性冲击"），LCS 仅 4 字、jaccard 0.15~0.20、无事件组/板块交集，
    # 且三条均 market 级（_topic_saturated 豁免），既有分支全部兜不住。
    # 新增：共享同一央行实体 + 标题均含宏观政策词 + 政策方向不冲突 → 同事件。
    # 守卫：①同一央行实体（防日本央行 vs 美联储误并）；②方向守卫（防
    # "考虑加息 vs 考虑降息"）；③标题均含政策词（防仅共享"日本央行"实体
    # 的两条无关央行新闻被误并）。
    if _central_bank_shared(ctx.ent_a, ctx.ent_b):
        if _policy_direction_conflict(ta, tb):
            return False
        if (any(w in ta for w in _MACRO_POLICY_EVENT_WORDS)
                and any(w in tb for w in _MACRO_POLICY_EVENT_WORDS)):
            return True
    return None


def _same_event_entity_anchor_rules(ctx: _SameEventCtx) -> bool | None:
    """实体锚定规则族：同实体（模糊重叠）+ 金额/短语锚/板块/剔除实体后共享内容

    返回 True=同事件 / False=金额冲突终止 / None=无判定。
    """
    ta, tb = ctx.ta, ctx.tb
    if not (ctx.ent_overlap and not _title_direction_conflict(ta, tb)):
        return None
    # 2026-08-11 修复：同实体 + 共享事件短语 → 同事件（跨日报道实体顺序相反、
    # 措辞差异大，LCS 兜不住：索尼×台积电合资三推实证）。金额守卫：
    # 双方金额均明确且无交集 → 不同事件（防"50亿建厂 vs 10亿回购"误并漏推）。
    if (ctx.num_a and ctx.num_b) and not (ctx.num_a & ctx.num_b):
        return False
    if any(p in ta and p in tb for p in _EVENT_PHRASE_ANCHORS):
        return True
    # 2026-08-13 修复：同实体 + 无事件组 + 同板块（sectors 交集≥2 词）
    # + 标题共享≥2字 → 同事件（四川算力政策同轮双推实证："建强成都平原
    # 算力核心区" vs "布局万卡级以上智算集群" 同实体同板块、无共享锚、
    # LCS 仅 2 字，既有分支全兜不住）。
    # 守卫：金额冲突已排除；sectors 交集≥2 防"英伟达AI芯片 vs 英伟达AI服务器"
    # 仅共享宽泛"AI"被误并（漏推守卫）。
    # 2026-08-13 二轮：sectors 用 _sectors_overlap（子串包含）替代精确交集，
    # 修复 LLM 抽板块"AI算力"vs"AI/算力"不一致导致的漏合并（上海算力补贴×2）。
    if len(_sectors_overlap(ctx.sec_a, ctx.sec_b)) >= 2 and _lcs_len(ta, tb) >= 2:
        return True
    # 2026-08-25 审核实证：同实体兄弟报道（字节豆包×2/小米玄戒×2/华为
    # 发布会×2）措辞差异大、无事件组/板块交集/LCS 不足，同轮重复推送。
    # 判据：实体重叠 + 剔除实体词后标题仍共享连续内容；共享≥6字专有
    # 主题词，或同板块时共享≥2字。避免"国际标准/美加贸易"4字通用短语
    # 把固态电池/磁性元件、关税抬升/谈判破裂两对不同事件误并。
    sa = _strip_entities(ta, ctx.ent_a)
    sb = _strip_entities(tb, ctx.ent_b)
    if sa and sb:
        s_lcs = _lcs_len(sa, sb)
        # 共享连续段 ≥6（专有主题词，如"发布豆包工作"）；或同板块 + ≥2
        #（玄戒/华为发布会类）。纯 ≥4 兜底会误并两对不同事件——
        # "固态电池国际标准" vs "磁性元件国际标准"（共享"国际标准"4字）、
        # "关税抬升至50" vs "美加贸易谈判破裂"（共享"美加贸易"4字）。
        if s_lcs >= 6 or (len(_sectors_overlap(ctx.sec_a, ctx.sec_b)) >= 1 and s_lcs >= 2):
            return True
    return None


def _same_event_title_similarity(ctx: _SameEventCtx) -> bool:
    """标题相似度兜底：LCS 连续子串与子序列阈值，同实体/同金额放宽阈值

    2026-08-11 修复：同主体(实体模糊重叠)或同金额时放宽标题相似阈值——
    跨日报道措辞差异大（"拟合资建厂" vs "批准成立合资企业"），
    但主体+金额一致，放宽 LCS 即可合并，防 48h 内重复推送
    （韩国5万亿基金×2、索尼台积电合资×3 实证）。
    放宽仅对"同实体/同金额"生效：SK海力士不同事件（扩产 vs 股东回报）
    标题无共享长段，仍不会被误并（漏推守卫）。
    """
    ta, tb = ctx.ta, ctx.tb
    shorter = min(len(ta), len(tb))
    if shorter < 8:
        return False
    same_anchor = ctx.ent_overlap or bool(ctx.num_a & ctx.num_b)
    # 误并守卫（2026-08-11 实证）："央行授权德银" vs "央行十五五规划"
    # 仅因实体名"中国人民银行"重复出现，LCS 子序列虚高至 0.52 被误并。
    # 同锚放宽前要求：剔除实体词后标题仍有 ≥3 字连续共享内容，
    # 即两条报道除主体外确实描述同一件事。
    if same_anchor:
        strip_a = _strip_entities(ta, ctx.ent_a)
        strip_b = _strip_entities(tb, ctx.ent_b)
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
    return sub >= sub_min and sub / shorter >= sub_ratio


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
        # 当日预筛候选（P7-1 2026-08-22 新增）：经 LLM 判定的全部重大候选（含方向+事件签名），
        # 供创业板择时 news_modifier 消费"已推送 ∪ 预筛候选"，覆盖全部重大候选而非仅强档推送。
        # 记录于 LLM 判定后的每一候选（无论最终推/不推）；pushed_events 仍为"实际已推送"权威。
        # [{**事件签名, "dir": str, "t": str, "pushed": bool}]
        "candidate_events": [],
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
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            files = data.get("files") or {}
            fobj = files.get(GIST_STATE_FILENAME)
            if fobj is not None:
                break
        except Exception as e:
            last_error = e
            logger.warning(f"Gist 读取第{attempt + 1}次失败: {e}")
            fobj = None
        if attempt < 2:
            logger.info(f"Gist 状态文件暂未读到（第{attempt + 1}次，可能命中旧快照），1s 后重试")
            time.sleep(1)
    # 2026-08-13 P0 修复（状态丢失事故根因）：3 次均未读到文件时必须报错，
    # 禁止"空状态运行后覆盖写回"——此前静默返回 "{}" 导致 19:03 轮把
    # 4759 seen/200 pending/62 pushed 历史去重记录全部冲掉。
    if fobj is None:
        raise RuntimeError(f"Gist 状态文件读取失败（3 次尝试均未读到文件）: {last_error}")
    content = fobj.get("content")
    try:
        state = json.loads(content)
    except (json.JSONDecodeError, TypeError) as e:
        # 2026-09-02 P0 根因修复：Gist API GET 的 content 字段在文件接近 ~900KB 时
        # 会被截断返回（fobj["truncated"]=True；实测 917KB 文件 content 截到约
        # 70.8 万字符即断，raw_url 下载同一文件完整可解析）——不是写入侧损坏。
        # run#1214（09-02 07:00 北京）起 21 连败即此问题：json.loads 截断串必抛。
        # 因此解析失败不直接 raise，先回退 raw_url 下载完整内容；仍失败才真正
        # raise（"禁止静默降级空状态"原则不变）。
        raw_url = fobj.get("raw_url")
        if not raw_url:
            logger.error(f"Gist 状态文件 JSON 解析失败且无 raw_url 可回退"
                         f"（内容 {len(content) if content else 0} 字符）: {e}")
            raise
        logger.warning(f"Gist content 解析失败（可能 API 截断，{len(content) if content else 0} 字符）: {e}；"
                       "回退 raw_url 下载完整内容")
        # raw.githubusercontent 同样有 CDN 缓存，追加时间戳参数强制绕过（同上方 API 缓存对策）
        sep = "&" if "?" in raw_url else "?"
        raw_resp = requests.get(f"{raw_url}{sep}ts={int(time.time() * 1000)}",
                                headers=headers, timeout=30)
        raw_resp.raise_for_status()
        state = json.loads(raw_resp.content.decode("utf-8"))
        logger.info(f"raw_url 回退成功（{len(raw_resp.content)} 字节），content 截断绕过")
    # 结构校验：解析成功但缺 seen 等核心键 → 视为损坏（防"{}"等畸形内容被当空状态）
    if not isinstance(state, dict) or "seen" not in state:
        raise ValueError("Gist 状态文件结构异常（缺少 seen 键），拒绝空状态运行")
    return state


def _gist_save(token: str, gist_id: str, state: dict) -> None:
    """将状态写回 Gist（单文件提交，防并发覆盖）。

    2026-08-31 修复：Gists API 不支持条件请求，ccbe890（8-29 P0-2）加入的
    If-Match 头自上线起即被 GitHub 一律拒绝（400），曾致云端零写入 62+ 小时。
    并发安全依赖 workflow concurrency 串行 + 单文件提交；patch_gist_file
    内部自带重试。禁止给 Gist PATCH 加 If-Match/If-None-Match。
    """
    patch_gist_file(
        GIST_STATE_FILENAME,
        json.dumps(state, ensure_ascii=False, indent=2),
        token, gist_id,
    )


def load_state() -> dict:
    """加载状态：云端优先 Gist，本地用文件"""
    gist_token, gist_id = get_gist_config()

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
            # Gist 已配置但不可读时，禁止回退旧本地状态，避免重复推送或状态回退。
            raise RuntimeError(f"Gist 读取失败，禁止无状态运行: {e}")

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


def _pending_same_as_pushed(pend_title: str, pushed_title: str) -> bool:
    """pending 清理用宽松同事件判定（归一化标题字符集 Jaccard 或 LCS 覆盖足够高）"""
    if not pend_title or not pushed_title:
        return False
    ja = len(set(pend_title) & set(pushed_title)) / len(set(pend_title) | set(pushed_title))
    if ja >= 0.55:
        return True
    shorter = min(len(pend_title), len(pushed_title))
    return shorter >= 6 and _lcs_len(pend_title, pushed_title) / shorter >= 0.5


def _pend_payload(n: dict) -> dict:
    """pending 挂起条目的全量 payload（2026-09-04 P1-1 主动重注入用）。

    溢出条目滚出源窗口（如财联社 20 条滚动窗）后，此前只能等重新被抓取
    （fp 相同）才重回判定，否则永久躺 pending 到 48h 过期。存全量 payload
    供下轮主动重注入。只保留预筛/LLM 判定所需字段，截断控 Gist 体积
    （中文 UTF-8 每字符 3 字节，200 条 × ~700B ≈ 140KB）。"""
    return {
        "title": str(n.get("title", "") or "")[:80],
        "content": str(n.get("content", "") or "")[:120],
        "source": str(n.get("source", "") or "")[:30],
        "published_at": str(n.get("published_at", "") or "")[:30],
    }


def _reinject_pending_items(pending: dict, news_list: list, seen: dict) -> list:
    """pending 主动重注入（2026-09-04 P1-1）：返回本轮未重新抓取到的挂起条目。

    只重注入带 payload 的记录（旧版无 payload 的 pending 记录跳过，等自然过期）；
    已在本轮 news_list 中重新抓到的条目走正常增量检测路径，不重复注入；
    已落 seen 的条目跳过。按 retry 从大到小排序（老条目优先进入判定）。"""
    if not pending:
        return []
    fetched_fps = {_news_fingerprint(n) for n in news_list}
    reinject = []
    for fp, rec in pending.items():
        if fp in seen or fp in fetched_fps:
            continue
        payload = rec.get("payload")
        if not payload:
            continue
        n = dict(payload)
        n["_fp"] = fp
        n["_pend_retry"] = int(rec.get("retry", 0))
        n["_from_pending"] = True
        reinject.append(n)
    reinject.sort(key=lambda x: x["_pend_retry"], reverse=True)
    return reinject


def _event_sig_key(e: dict) -> str:
    """事件签名内容键（状态合并去重用）"""
    return ("|".join(sorted(e.get("entities") or [])) + "#"
            + "|".join(sorted(e.get("events") or [])) + "#"
            + "|".join(sorted(e.get("numbers") or [])) + "#"
            + (e.get("title_norm") or ""))


def _day_key(e: dict) -> str:
    """事件所在日期（YYYY-MM-DD，取 t 字段前缀）"""
    return str(e.get("t") or "")[:10]


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

    # 合并当日预筛候选（P7-1）：按 (日期, 事件签名) 去重，pushed=True 优先（并发写防丢方向）。
    # 并发实例各自读-改-写时，直接覆盖会把另一方新增的候选方向丢掉 → 合并后冲突窗口收窄到单次写入。
    merged_cands = {}
    for e in (remote.get("candidate_events") or []):
        merged_cands[(_day_key(e), _event_sig_key(e))] = e
    for e in (local.get("candidate_events") or []):
        _k = (_day_key(e), _event_sig_key(e))
        old = merged_cands.get(_k)
        if old is None or (e.get("pushed") and not old.get("pushed")):
            merged_cands[_k] = e
    local["candidate_events"] = list(merged_cands.values())
    return local


def save_state(state: dict) -> None:
    """保存状态：云端写 Gist（读-改-写合并防并发覆盖），本地写文件"""
    gist_token, gist_id = get_gist_config()

    _merge_failed = False
    if gist_token and gist_id:
        # 写入前先合并最新远端状态，避免并发实例互相覆盖
        try:
            latest = _gist_load(gist_token, gist_id)
            state = _merge_state(state, latest)
        except Exception as e:
            # 2026-08-13 P0 修复（状态丢失事故根因）：合并失败禁止静默覆盖。
            # 此前"直接覆盖写入"导致 19:03 轮把 Gist 中 4759 条历史 seen 全部冲掉。
            # CI 下报错退出（fail-stop：宁可本轮失败，不可丢去重基准）；
            # 本地模式下拒绝覆盖 Gist，降级写本地文件（防远端被冲，用户可手动恢复）。
            logger.error(f"Gist 保存前合并失败: {e}")
            _merge_failed = True
            if _is_ci():
                raise RuntimeError(f"Gist 合并失败，拒绝覆盖写入（防止状态丢失）: {e}")
            logger.warning("本地模式合并失败：拒绝覆盖 Gist（防远端去重基准被冲），状态降级写入本地文件")
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
    # 2026-08-13 P0：seen 无上限导致状态文件膨胀至 0.72MB（Gist 写入截断损坏诱因）。
    # 48h 清理后仍超上限时按上限裁剪，优先淘汰未推条目（见 _prune_seen）。
    # 2026-09-01 修正触发条件：条数上限不是唯一闸门——纯条数未超但字节已超
    # 700KB 时同样裁剪（中文 UTF-8 下 12000 条可能超 1MB Gist 硬限）。
    if len(seen) > SEEN_MAX or _est_seen_bytes(seen) > SEEN_MAX_BYTES:
        before_n = len(seen)
        seen = _prune_seen(seen, SEEN_MAX)
        logger.warning(f"seen 超过上限（{before_n} 条 → {len(seen)} 条），"
                       "优先淘汰未推条目后按时间保留最新（条数/字节双上限）")
    state["seen"] = seen

    # 滚动清理过期挂起重试（48h 窗口）+ 上限 200 条防爆胀
    pending = state.get("pending", {})
    pend_expired = [fp for fp, rec in pending.items() if rec.get("t", "") < cutoff]
    for fp in pend_expired:
        pending.pop(fp, None)
    if len(pending) > 200:
        pending = dict(sorted(pending.items(), key=lambda kv: kv[1].get("t", ""))[-200:])
    # 2026-09-04 P1-1：pending 带 payload 后按字节兜底（Gist 1MB 硬限）
    if _est_pending_bytes(pending) > PENDING_MAX_BYTES:
        pending = dict(sorted(pending.items(), key=lambda kv: kv[1].get("t", ""))[-150:])
    state["pending"] = pending
    if pend_expired or len(pending) != len(state.get("pending", {})):
        logger.info(f"清理过期挂起重试 {len(pend_expired)} 条，剩余 {len(pending)} 条")

    # 滚动清理过期已推事件签名（48h 窗口）+ 上限 300 条防爆胀
    pe = [e for e in (state.get("pushed_events") or []) if e.get("t", "") >= cutoff]
    if len(pe) > 300:
        pe = sorted(pe, key=lambda e: e.get("t", ""))[-300:]
    state["pushed_events"] = pe

    # 滚动清理过期当日预筛候选（P7-1，48h 窗口）+ 上限 300 条防爆胀
    ce = [e for e in (state.get("candidate_events") or []) if e.get("t", "") >= cutoff]
    if len(ce) > 300:
        ce = sorted(ce, key=lambda e: e.get("t", ""))[-300:]
    state["candidate_events"] = ce

    if gist_token and gist_id and not _merge_failed:
        _gist_save(gist_token, gist_id, state)
        logger.info(f"状态已保存到 Gist（{len(seen)} 个指纹, {len(pe)} 个已推事件）")
        return

    # 无 Gist 配置 或 合并失败降级（本地模式）→ 写本地文件
    state_path = _state_path()
    atomic_write_json(state_path, state)
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


# 大额经营事件动作词（2026-08-13 修复：预筛漏判实证——"宁德时代签订200亿储能订单"0.19、
# "SK海力士重启中国NAND工厂"0.39、"贵州茅台中标50亿工程"0.14 均因词表缺动作词被拦。
# 中标/签约/建厂/扩产等是"重大经营事件"信号，与 HIGH_SIGNAL 的并购/重组同级，
# 但刻意不并入 HIGH_SIGNAL_KEYWORDS（避免扩大指纹 sig 路径影响面——
# 这些词直通 LLM 判定即可，是否推送仍由 LLM 把关龙头/重要性）。
_DEAL_ACTION_WORDS = [
    "中标", "签约", "签订", "签署", "斩获", "拿下",
    "大额订单", "重大合同", "重大工程", "建厂", "投建", "投产", "扩产", "扩建",
    "重启", "量产", "增资", "落地",
]
_DEAL_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*亿")


def _is_major_deal_event(text: str) -> bool:
    """大额经营事件识别：动作词 + (金额≥1亿 或 科技硬件词) → 直通 LLM 判定

    只影响中小市值个股自身的中标/签约按用户口径不推，但预筛阶段无法知道
    谁是龙头，故"大额经营事件"统一送 LLM，由 LLM 判断龙头与否（与"回购/并购"
    等资本运作词的处理一致）。小额（<1亿）且非科技的经营动作不直通，避免
    中小市值日常经营消息挤占候选。
    """
    if not text:
        return False
    if not any(a in text for a in _DEAL_ACTION_WORDS):
        return False
    if _DEAL_AMOUNT_RE.search(text):
        return True
    if _has_tech_keyword(text):
        return True
    return False


# 预筛直通主体词表（2026-08-13 修复：漏推实证——"DeepSeek V4 Pro 正式版上线"、
# "长江存储首次跻身全球NAND前三"、"寒武纪五大国产模型适配"、"腾讯营收首超2000亿"、
# "钙钛矿量产技术登《自然》" 均预筛 0.14 被静默丢弃，根因是 HIGH_SIGNAL 词表缺
# 具体科技龙头/重要主体名。本表只用于预筛直通（命中即送 LLM 判定），刻意不并入
# HIGH_SIGNAL_KEYWORDS——避免这些宽泛主体词进入指纹 sig 路径造成不同事件指纹碰撞。
# 是否推送仍由 LLM 严格判定（日常/行情/中小市值消息由 LLM 否决）。
_PREFILTER_HEADLINE_ENTITIES = [
    # 国产 AI/算力/科技龙头
    "DeepSeek", "寒武纪", "中际旭创", "长江存储", "长鑫存储", "华为", "海思",
    "腾讯", "阿里", "阿里巴巴", "字节", "百度", "小米", "比亚迪", "宁德时代",
    # 全球科技/半导体龙头
    "英伟达", "台积电", "三星", "SK海力士", "海力士", "美光", "铠侠", "ASML",
    "OpenAI", "Anthropic", "微软", "谷歌", "苹果", "特斯拉", "Meta", "亚马逊",
    # 新能源/新材料技术
    "钙钛矿", "固态电池",
]


def _hit_headline_entity(text: str) -> bool:
    """是否命中预筛直通主体词（科技龙头/重要主体）"""
    if not text:
        return False
    return any(e in text for e in _PREFILTER_HEADLINE_ENTITIES)


def _prefilter(news: dict) -> tuple:
    """规则预筛：返回 (预筛评分, 是否命中高信号词)

    2026-08-12 修复：高信号词命中 或 宏观数据发布识别（_is_macro_data_release）
    均直通 LLM 判定——确定性宏观数据事件即使预筛分低也不允许静默丢弃。
    2026-08-13 修复：大额经营事件（_is_major_deal_event）与科技龙头主体词
    （_hit_headline_entity）同样直通，防龙头重大订单/发布/里程碑被预筛静默丢弃。
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
    hit = (has_signal_keyword(text) or _is_macro_data_release(text)
           or _is_major_deal_event(text) or _hit_headline_entity(text))
    return float(score), hit


# ============================================================
# LLM 快速重要性判定
# ============================================================
_LLM_SYSTEM_PROMPT = """你是A股资讯重要性审核员。判断每条资讯是否属于"必须立即推送的重大消息"。
推送优先级由高到低，以下任一条件成立则应判为推送：
1. 全球流动性/利率/汇率/套息/资金面事件（驱动万亿级量化资金系统性调仓的因子信号，market 级必推）：
   - 日本央行货币政策（加息/降息/YCC 调整/购债变化/植田和男等重要官员释放政策转向信号）及
     日元急升急贬——日元是全球核心融资货币，日本加息会引发日元套息交易(carry trade)平仓、
     全球风险资产去杠杆，是 A 股大跌的直接导火索（2026-08-13 实证漏推：午后"日本首相支持央行
     近期加息"引发日元急升、A股尾盘跳水超 4300 股下跌，但系统全天未推任何日本/日元/套息资讯）
   - 美债收益率异动（10 年期/2 年期/30 年期中标收益率创阶段新高/新低、收益率曲线倒挂/陡峭化）、
     美国财政部拍卖结果、中美利差变化
   - 人民币/离岸人民币汇率急变、美元指数异动、央行汇率干预
   - 中国央行流动性操作与资金面：逆回购/MLF/买断式逆回购/降准降息/公开市场净投放净回笼、
     社融/M2/信贷等货币信用数据发布
   - 市场波动率与对冲成本：VIX 急升、股指期货基差/贴水大幅走扩或收敛（直接决定中性策略
     对冲成本与仓位）、融资余额/两融/北向资金等资金流大额异动
   以上即使来自"外围央行"或"官员表态"，只要涉及重大政策转向或直接影响全球/A 股流动性，
   均属 market 级必推，不适用下方"外围央行日常表态不推"条款
   （2026-08-14 补充：同一央行政策动作的多源报道/衍生快讯——如"日本央行考虑9月加息"
   "加息或提速美元对日元急跌""消息人士称最快可能在9月加息"——属同一事件的多个角度，
   判定时按同一事件口径处理，不要因角度不同而各自判为独立重大消息）
2. 影响整个市场/大盘（宏观政策、央行、证监会、国常会、政治局会议、重大地缘政治事件；
   以及中国/美国核心宏观数据发布——CPI、PPI、非农、GDP、利率决议、PMI 等——
   数据发布本身即 market 级重大消息，方向按数据对市场的实际含义判定，
   公布后的市场反应（股债汇、加息/降息预期变化、机构点评）同属重大，与数据本体
   合并为一条推送即可，不必每条机构点评都单独推）
3. 影响科技板块/科技产业链的资讯（AI、算力、半导体、芯片、存储、光模块/CPO、PCB、MLCC、
   机器人、消费电子等）：板块景气变化、龙头动向、技术突破、产业政策——即使标题未点名个股
4. 科技龙头个股的重大消息（寒武纪、中际旭创、宁德时代、英伟达产业链相关等第一梯队
   公司的重大经营事件、大额订单、业绩剧变、监管动向）
5. 外围（美股/港股/国际宏观/地缘）消息，若其直接影响A股大盘或科技板块
6. AI/科技龙头的新产品、新模型、新芯片发布（OpenAI/微软/英伟达/谷歌/三星/SK海力士等发布
   新模型版本、自研芯片、HBM新品等）——只要消息经证实或来自权威媒体，即属重大，即使
   没有点名A股公司（2026-08-11 实证漏推：OpenAI发布GPT-5.6-Cyber）。
   ⚠重大性限定（2026-08-25 实证虚推）：仅旗舰级/生态级产品属重大——数据中心级芯片、
   HBM/存储新品、大模型版本、自研旗舰SoC、直接改变产业链格局（光模块/CPO/算力链）的产品；
   入门级/边缘侧/开发者套件类的常规产品线更新（如 Jetson 入门款边缘计算机、配件迭代、
   开发者工具）不改变产业链格局，不属板块级重大——scope 最多 stock 级且 score ≤5 不推，
   除非其中包含影响核心算力链的实质性技术跨越（推理成本数量级下降等）
7. 核心科技板块的利空警示（行业见顶信号、龙头目标价被大幅下调、产能过剩担忧、重大诉讼/
   监管审查）——利空警示同样属于必须推送的重大消息，方向为 bearish（2026-08-11 实证漏推：
   韩国券商砍三星/SK海力士目标价约30%）
8. AI 监管与政策（立法机构、监管机构、政府要员对 AI 开发/使用/出口的限制、调查、听证）——
   即使不点名具体公司（2026-08-11 实证漏推：美参议员致信要求暂停AI开发）
明确不推（无论业绩多好、涨跌多剧烈）：
- 纯个人观点/猜测类言论：政客或机构单方面"怀疑""认为""预计"等表态，没有真实事件或官方立场
  变化、没有实际市场反应佐证，则不视为重大事件——即便话题涉及地缘、石油或美股
- ⚠️地缘/军事"事实层"豁免（2026-08-31 实证漏推：特朗普称将对伊朗袭击美军事件作出回应，
  4 条源全被拒，次日美伊交火、布油站上 90 美元才补推）：已发生的军事行动及其官方确认——
  袭击/交火/空袭/导弹打击/拦截/伤亡确认/一方宣布将采取军事报复——不是"观点表态"，
  属第 2 条"重大地缘政治事件"必推（同一冲突事件的多源/多角度快讯按同一事件口径合并判定）。
  只有未伴随新事实的口头警告、预期管理、立场重申才适用上方"表态不推"条款
- 只影响中小市值个股自身股价的消息：业绩预告/业绩变动、小额回购、增持/减持、中标/签约、
  日常经营、子公司事项、分红送转等——除非该股是行业龙头或直接改变板块逻辑
- ⚠️龙头大额资本运作必推（2026-08-31 实证漏推：中际旭创拟 40-80 亿元回购 21:34 被拒、
  次日 06:02 换措辞才推，延迟 9 小时）：核心科技龙头（寒武纪、中际旭创、宁德时代、
  英伟达产业链第一梯队等）的回购/增持计划金额 ≥10 亿元，属第 4 条"科技龙头重大消息"
  必推——判定看行业第一梯队属性（如"光模块巨头"），不要因消息是回购/增持类就按
  "中小市值消息"条款降级
- 外围央行（非中国）的日常表态/会议纪要/储备数据（印度央行、匈牙利央行、澳洲联储等的例行表态），
  除非涉及重大政策转向或直接影响 A 股；注意：①此条不含宏观数据发布本身——
  外围重要数据（美国 CPI/PPI/非农/利率决议等）因直接影响全球市场与 A 股，仍按优先级 1 推送；
  ②日本央行加息/降息/YCC 调整及日元套息交易动向不属于"日常表态"——日元是全球核心融资货币，
  日本货币政策转向会通过套息平仓直接冲击 A 股与全球风险资产，按优先级 1 必推（2026-08-13 实证漏推）
- 分析师评级调整/目标价小幅变动（杰富瑞、伯恩斯坦等），除非幅度极大且已引发市场剧烈反应
- 券商研报/机构观点/主题性分析：无具体事件佐证的定性判断——"机构称""研报""XX投资机会"
  "XX向XX传导""XX进入XX期""XX有望受益""XX空间广阔"等，即使涉及科技/AI/算力板块也不推；
  必须是已发生的具体事件（硬数据如"台积电CoWoS良率升至98%"、明确订单金额、政策动作、公司公告）
  才可能构成重大（2026-08-13 实证滥推："机构称物理AI市场空间""液冷投资向零部件传导"等）
- 无官方确认的产品传闻/路线图预测（苹果折叠 iPhone、郭明錤预测等）
- 与上述优先级均无关的其他资讯

对每条输入严格输出一个 JSON 数组元素，字段：
{"idx": 输入的idx原样回显, "title": "原标题", "push": true/false, "score": 0到10的整数, "direction": "bullish|mildly_bullish|neutral|mixed|mildly_bearish|bearish",
 "scope": "market|sector|stock", "sectors": ["板块名"], "entities": ["事件主体公司/机构规范简称，1-3个，无则空数组"], "is_leader_stock": true/false,
 "env_note": "与当前量化环境的关系标注", "reason": "一句话理由"}
direction 必须区分强度：只有影响显著且方向明确才用 bullish/bearish（强档）；
小幅波动用 mildly_bullish/mildly_bearish；方向不明用 neutral/mixed。
is_leader_stock: 仅当该资讯主体是行业龙头个股（市值/地位第一梯队）时为 true，否则 false。
entities: 事件的当事公司/机构/人物规范简称（如"恩智浦""安霸""美联储"），用于跨源同事件去重，必须与标题所述主体一致。
env_note: 仅当用户消息开头给出【当前量化环境】时输出——资讯方向与量化环境同向标"共振: ..."（一句话）；
反向标"背离: ..."并提示谨慎（如"背离: 利好消息但风险收缩期，量化资金在降仓，谨慎对待"）；
环境缺失或该资讯与量化资金环境无关时输出空字符串。env_note 只描述事件与量化环境的关系，
不得包含任何买卖建议。
related_recent: 输入行可能带 related_recent 字段（近48h同主体的已推事件及方向）——这是叙事链上下文：
同主体出现反向事件（叙事反转，如连续利好后突发利空）属信号增强，可在事实清楚时上调 score；
同向事件为叙事延续，本条 direction 仍按本条自身事实独立判定，不要因为先前事件方向而改变本条方向。
注意: 输出必须是合法 JSON，字符串内的双引号必须转义为 \"（或改用「」）；idx 必须原样回显。
不要输出任何 JSON 以外的文字。"""


def _build_llm_user_prompt(items: list, env_context: str = "", pushed_events: list = None) -> str:
    """构造用户 prompt（P2-2：env_context 非空时头部注入量化环境，供共振/背离标注）

    P6-3（2026-08-19）：条目存在近 48h 同主体已推事件时注入 related_recent 字段
    （叙事链上下文，多数条目无匹配不注入，prompt 增量可控）。
    """
    lines = []
    for n in items:
        row = {
            "idx": n.get("_judge_idx"),
            "title": str(n.get("title", ""))[:80],
            "content": str(n.get("content", "") or "")[:200],
            "published_at": str(n.get("published_at", "")),
        }
        related = _related_recent_note(n, pushed_events or [])
        if related:
            row["related_recent"] = related
        lines.append(json.dumps(row, ensure_ascii=False))
    header = f"【当前量化环境】{env_context}\n\n" if env_context else ""
    return header + "请逐条审核以下资讯（不要遗漏任何一条，idx 原样回显）:\n[\n" + ",\n".join(lines) + "\n]"


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
        "env_note": "",
        "reason": "LLM未判定，挂起下轮重试",
    }


def _llm_judge(items: list, deadline: float = 0, env_context: str = "",
               pushed_events: list = None) -> list:
    """批量 LLM 判定，返回与 items 一一对应的判定 dict 列表

    Args:
        items: 待判定候选
        deadline: 总超时熔断时间戳(time.monotonic())，0=不限。
                  2026-08-06 修复：此前调用 _call_llm_api 未传 deadline，
                  多批(最多5批)×90s×2次重试最坏 900s，突破 GitHub Actions
                  timeout-minutes:10 导致任务被强杀（已推送未保存状态→重复推送）。
                  现在批次循环入口检查熔断，逼近 deadline 立即挂起剩余批次。
        env_context: 量化环境上下文（P2-2，_llm_env_context 生成）。
                  非空时注入用户 prompt 头部，LLM 输出 env_note 共振/背离标注；
                  空串时不注入（快照缺失/过期，向后兼容）。
        pushed_events: 已推事件（P6-3 叙事链，条目命中同主体近48h事件时
                  注入 related_recent 字段，None/空不注入）。

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
            raw = _call_llm_api(_LLM_SYSTEM_PROMPT, _build_llm_user_prompt(batch, env_context, pushed_events), timeout=60, max_retries=1, deadline=deadline)
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
                    "sectors": _as_list(e.get("sectors")),
                    "entities": [str(x).strip() for x in _as_list(e.get("entities"))
                                 if isinstance(x, (str, int, float)) and str(x).strip()],
                    "is_leader_stock": _as_bool(e.get("is_leader_stock", False), False),
                    "env_note": str(e.get("env_note", "") or "").strip()[:80],
                    "reason": str(e.get("reason", "") or "").strip(),
                }
            logger.info(f"LLM 判定批次 {start//LLM_BATCH_SIZE + 1}: {len(batch)} 条完成（回显{len(entries)}条）")
        except Exception as e:
            logger.warning(f"LLM 判定批次失败（{len(batch)} 条），整批挂起下轮重试: {e}")
            for offset, n in enumerate(batch):
                results[start + offset] = _hang_judge(n)
        # 2026-08-13 P2 修复：批次完成后检查剩余预算——此前只在批次入口检查，
        # 单批最坏 90s×2+2s=182s，300s 预算实际可超（8-07 日志实证单轮 380s）。
        # 批次完成即复查 deadline，逼近则立即挂起剩余批次。
        if deadline and time.monotonic() >= deadline:
            rest = list(range(start + LLM_BATCH_SIZE, len(items)))
            if rest:
                logger.warning(f"LLM 判定批次完成后超时熔断，剩余 {len(rest)} 条挂起下轮重试")
                for r in rest:
                    results[r] = _hang_judge(items[r])
            break
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


def _tech_override_enabled(news: dict, judge: dict, leader_watchlist: set) -> bool:
    """科技兜底放行判定（2026-08-13 P0 重构：从 run_once 5c 提取，便于测试）

    防 LLM 漏推科技硬事件：scope=sector 且含国内科技词（或 stock 且龙头且科技）
    时，LLM 判 push=false 也兜底放行。但命中研报/观点/主题措辞
    （_TECH_OVERRIDE_VIEW_WORDS，如"机构称/市场空间/传导/开启"）的定性判断不放行——
    实测"机构称物理AI市场空间"等研报/观点类曾因此被强制放行误推，让 LLM 的
    push=false 否决恢复生效。
    """
    sectors = _as_list(judge.get("sectors"))
    scope = str(judge.get("scope", "stock") or "stock").lower()
    is_leader = bool(judge.get("is_leader_stock")) or _hit_watchlist(news, leader_watchlist)
    strong = str(judge.get("direction", "neutral")) in ("bullish", "bearish")
    score_ok = _to_float(judge.get("score", 0)) >= 5
    view_text = f"{str(news.get('title', ''))} {str(news.get('content', '') or '')}"
    if any(w in view_text for w in _TECH_OVERRIDE_VIEW_WORDS):
        return False
    if scope == "sector" and _is_domestic_tech(news, sectors) and (score_ok or strong):
        return True
    if scope == "stock" and is_leader and _is_domestic_tech(news, sectors) and (score_ok or strong):
        return True
    return False


# ============================================================
# 风险收缩期联动（2026-08-14 第二阶段）
# factor_collector 检测到贴水走扩/日元急升/放量破位 → risk_off 时，
# 资讯管线对"无硬事件佐证的科技利好"降级不推，与量化资金风险期降杠杆一致；
# 利空/风险资讯不受影响（风险期用户更关心利空）。
# ============================================================
_FACTOR_STATE_PATH = PROJECT_ROOT / "logs" / "factor_state.json"

# 硬事件佐证词（已发生的完成态动作）：命中任一即视为"具体事件"（放行）。
# 2026-08-14 实证调优：移除过宽的"订单/协议/合作/投资/业绩"（常见于"迎订单验证窗口"
# "战略合作""机构投资"等展望/情绪语境，会误放行）；"扩建/投运/良率/产能"等补入。
_HARD_EVENT_WORDS = (
    "中标", "签约", "签署", "获批", "收购", "并购", "入股", "回购", "增持",
    "量产", "扩产", "扩建", "投产", "建成", "投运", "上线", "发布", "拿下",
    "交付", "落户", "良率", "产能", "出货",
)
# 展望修饰词：硬事件动词前 6 字内出现 → 视为"展望/未发生"而非已发生事件，
# 不构成硬事件佐证（如"有望中标""拟收购""订单验证窗口"）。
_FORWARD_MODIFIERS = ("有望", "预期", "预计", "或", "计划", "拟", "验证", "窗口", "开启", "迎")


def _is_tech_by_sectors(sectors) -> bool:
    """板块是否科技类（降级判定专用宽词表 TECH_SECTOR_WORDS_WIDE，单一数据源）"""
    secs = " ".join(str(s) for s in (sectors or []))
    return any(kw in secs for kw in TECH_SECTOR_WORDS_WIDE)


def _load_factor_state() -> dict:
    """读取 factor_collector 的完整因子状态：配置 Gist 时云端唯一来源，否则读本地文件

    缺失/解析失败 → {}（联动与市场环境行均为增强功能，失败不影响资讯主流程）。
    云端未跑 factor_collector 时读不到 → 返回空 dict，保证向后兼容。
    """
    gist_token, gist_id = get_gist_config()
    if gist_token and gist_id:
        try:
            import requests
            url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
            headers = {"Authorization": f"token {gist_token}",
                       "Accept": "application/vnd.github+json",
                       "User-Agent": "stock-news-agent-realtime"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            files = resp.json().get("files") or {}
            fobj = files.get("factor_state.json")
            if fobj is not None:
                state = json.loads(fobj.get("content") or "{}")
                return state if isinstance(state, dict) else {}
        except Exception as e:
            # 联动是增强功能，读取失败降级为空，不使用本地旧状态
            logger.debug(f"Gist 因子状态读取失败，增强联动降级为空: {e}")
        return {}
    try:
        state = json.loads(_FACTOR_STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _load_factor_risk_state() -> str:
    """读取 factor_collector 的风险状态（_load_factor_state 的便捷封装）

    缺失/解析失败/非预期值 → neutral（不影响现有推送）。云端未跑 factor_collector
    时读不到 → 联动自动失效，保证向后兼容。
    """
    rs = _load_factor_state().get("risk_state", "neutral")
    return rs if rs in ("risk_off", "neutral") else "neutral"


# 市场环境行快照过期阈值（小时）：factor_collector 盘中 15 分钟/盘后 60 分钟一轮，
# 48h 覆盖周末（周五收盘快照在周末推送时仍有参考价值，超 48h 视为过期省略）。
_FACTOR_ENV_MAX_AGE_HOURS = 48


# P10（2026-08-20）风险档位判定阈值：中性状态下的警戒信号组合
_WARN_MAIN_OUTFLOW_YI = 500   # 主力净流出警戒（亿元）
_WARN_GC007 = 3.0            # 资金面利率警戒（%）
_WARN_PCR = 1.5              # 期权恐慌警戒

# 指数显示简称（环境行紧凑用）
_INDEX_SHORT = {"上证指数": "上证", "创业板指": "创业板"}


def _market_risk_grade(snapshot: dict) -> str:
    """风险档位（三档，始终显示）：🔴风险收缩 / 🟡警戒 / 🟢中性

    P10（2026-08-20）：此前 risk_state 仅 risk_off 时显示"⚠️风险收缩期"、
    neutral 静默——用户看到一堆数字却不知综合是安全还是危险。
    现拆三档：risk_off=收缩；neutral 但命中任一警戒信号（高波/极端普跌/
    主力大额流出/资金面收紧/期权恐慌）=警戒；否则=中性。
    """
    if snapshot.get("risk_state") == "risk_off":
        return "🔴风险收缩"
    if any(isinstance(v, dict) and v.get("regime") == "高波"
           for v in (snapshot.get("vol") or {}).values()):
        return "🟡警戒"
    dp = (snapshot.get("breadth") or {}).get("down_pct")
    if isinstance(dp, (int, float)) and dp >= 80:
        return "🟡警戒"
    mn = (snapshot.get("flows") or {}).get("main_net_yi")
    if isinstance(mn, (int, float)) and mn <= -_WARN_MAIN_OUTFLOW_YI:
        return "🟡警戒"
    gc = ((snapshot.get("liquidity") or {}).get("gc007") or {}).get("price")
    if isinstance(gc, (int, float)) and gc >= _WARN_GC007:
        return "🟡警戒"
    pcr = (snapshot.get("option") or {}).get("pcr")
    if isinstance(pcr, (int, float)) and pcr >= _WARN_PCR:
        return "🟡警戒"
    return "🟢中性"


def _factor_env_line(snapshot: dict, now: datetime = None) -> str:
    """把因子快照压成单行"市场环境"（附在资讯推送卡片，P0-1 融合展示）

    P10（2026-08-20）重构：①风险档位三档始终显示（🔴收缩/🟡警戒/🟢中性）
    ②补创业板指（与上证并列）③情绪档位/GC007/期权PCR 始终显示（极端值加⚠）
    ④市场宽度始终显示（跌X%）——此前仅极端/风险态才显示，用户无法判断常态。

    快照缺失/字段空/超过 48h 过期 → 返回 ""（推送退化为原格式，不带该行）。
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return ""
    ts = str(snapshot.get("ts", "") or "")
    try:
        snap_time = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=BJT)
    except ValueError:
        return ""
    now = now or datetime.now(BJT)
    if snap_time > now:
        return ""
    if (now - snap_time).total_seconds() > _FACTOR_ENV_MAX_AGE_HOURS * 3600:
        return ""

    parts = []
    basis = snapshot.get("basis") or {}
    basis_parts = []
    for code in ("IC", "IM"):
        b = basis.get(code) or {}
        pct = b.get("basis_pct")
        if isinstance(pct, (int, float)):
            basis_parts.append(f"{code} {pct:+.2f}%")
    if basis_parts:
        parts.append("贴水 " + " ".join(basis_parts))
    fx = snapshot.get("fx") or {}
    jpy = fx.get("美元/日元") or {}
    if isinstance(jpy.get("change_pct"), (int, float)):
        parts.append(f"美元/日元 {jpy['change_pct']:+.2f}%")
    indexes = snapshot.get("indexes") or {}
    idx_parts = []
    for ix_name in ("上证指数", "创业板指"):
        ix = indexes.get(ix_name) or {}
        if isinstance(ix.get("change_pct"), (int, float)):
            arrow = "▲" if ix["change_pct"] >= 0 else "▼"
            idx_parts.append(f"{_INDEX_SHORT[ix_name]}{arrow}{ix['change_pct']:+.2f}%")
    if idx_parts:
        parts.append(" ".join(idx_parts))
    # P1-3（2026-08-19）：两市主力净流入（快照无 flows 键时省略，旧快照兼容）
    flows = snapshot.get("flows") or {}
    mn = flows.get("main_net_yi")
    if isinstance(mn, (int, float)) and mn != 0:
        parts.append(f"主力{'净流入' if mn > 0 else '净流出'}{abs(mn):.0f}亿")
    # P3（2026-08-19）：隔夜外盘（AI硬件链先行指标）
    gq = snapshot.get("global") or {}
    for g_name in ("纳斯达克100", "英伟达"):
        g = gq.get(g_name) or {}
        if isinstance(g.get("change_pct"), (int, float)):
            flag = "⚠️" if abs(g["change_pct"]) >= 2.0 else ""
            parts.append(f"{flag}隔夜{g_name.replace('纳斯达克100', '纳指')}{g['change_pct']:+.2f}%")
    # P12（2026-08-21）：韩指 KOSPI（存储链先行；14:30 BJT 收盘早于 A 股，盘中为
    # 实时信号故不带"隔夜"前缀）；P10 常态不显示口径——|涨跌|≥2% 才上环境行
    g = gq.get("韩国KOSPI") or {}
    if isinstance(g.get("change_pct"), (int, float)) and abs(g["change_pct"]) >= 2.0:
        parts.append(f"⚠️韩KOSPI{g['change_pct']:+.2f}%")
    # 市场宽度（P10 始终显示：跌X%）
    dp = (snapshot.get("breadth") or {}).get("down_pct")
    if isinstance(dp, (int, float)):
        flag = "⚠️" if dp >= 80 else ""
        parts.append(f"{flag}跌{dp:.0f}%")
    # 涨停情绪档位（P10 始终显示，亢奋/冰点加 emoji）
    mood = (snapshot.get("sentiment") or {}).get("mood")
    if mood:
        emoji = "🔥" if mood == "亢奋" else ("❄️" if mood == "冰点" else "")
        parts.append(f"{emoji}情绪{mood}")
    # 资金面利率 GC007（P10 始终显示，≥3% 加⚠）
    gc = ((snapshot.get("liquidity") or {}).get("gc007") or {})
    if isinstance(gc.get("price"), (int, float)):
        tight = gc["price"] >= _WARN_GC007
        parts.append(f"{'⚠️' if tight else ''}GC007 {gc['price']:.2f}%")
    # 期权情绪 PCR（P10 始终显示，≥1.5 加⚠）
    pcr = (snapshot.get("option") or {}).get("pcr")
    if isinstance(pcr, (int, float)):
        pcr_mood = "恐慌" if pcr >= 1.3 else ("看涨" if pcr <= 0.55 else "中性")
        parts.append(f"{'⚠️' if pcr >= _WARN_PCR else ''}PCR {pcr:.2f}({pcr_mood})")
    # 高波状态（仅命中时显示）
    if any(isinstance(v, dict) and v.get("regime") == "高波"
           for v in (snapshot.get("vol") or {}).values()):
        parts.append("⚠️高波")
    if not parts:
        return ""
    return f"**市场环境**({ts}): {_market_risk_grade(snapshot)} | " + " | ".join(parts)


def _llm_env_context(snapshot: dict, now: datetime = None) -> str:
    """因子快照 → LLM 判定环境上下文（P2-2 共振/背离标注）

    与 _factor_env_line 同一过期规则（>48h 失效）；失效/缺失 → ""，
    LLM 判定退化为无环境模式（向后兼容：云端 factor_collector 未跑/快照过期不受影响）。
    例："（08-19 15:00）风险收缩期（量化资金防守/降杠杆）；IC贴水-0.92%、IM贴水-0.65%
    （中性策略对冲成本）；美元/日元-0.43%；上证指数-2.40%；两市主力净流出1940亿"
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return ""
    ts = str(snapshot.get("ts", "") or "")
    try:
        snap_time = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=BJT)
    except ValueError:
        return ""
    now = now or datetime.now(BJT)
    if snap_time > now:
        return ""
    if (now - snap_time).total_seconds() > _FACTOR_ENV_MAX_AGE_HOURS * 3600:
        return ""
    parts = []
    if snapshot.get("risk_state") == "risk_off":
        parts.append("风险收缩期（量化资金防守/降杠杆）")
    basis = snapshot.get("basis") or {}
    basis_parts = []
    for code in ("IC", "IM"):
        b = basis.get(code) or {}
        pct = b.get("basis_pct")
        if isinstance(pct, (int, float)):
            basis_parts.append(f"{code}贴水{pct:+.2f}%")
    if basis_parts:
        parts.append("、".join(basis_parts) + "（中性策略对冲成本）")
    fx = snapshot.get("fx") or {}
    jpy = fx.get("美元/日元") or {}
    if isinstance(jpy.get("change_pct"), (int, float)):
        parts.append(f"美元/日元{jpy['change_pct']:+.2f}%")
    indexes = snapshot.get("indexes") or {}
    for ix_name in ("上证指数", "创业板指"):
        ix = indexes.get(ix_name) or {}
        if isinstance(ix.get("change_pct"), (int, float)):
            parts.append(f"{ix_name}{ix['change_pct']:+.2f}%")
    flows = snapshot.get("flows") or {}
    mn = flows.get("main_net_yi")
    if isinstance(mn, (int, float)) and mn != 0:
        parts.append(f"两市主力净{'流入' if mn > 0 else '流出'}{abs(mn):.0f}亿")
    # P3（2026-08-19）：外盘/波动率/宽度——LLM 判定共振/背离的增量上下文
    # P12：加韩国KOSPI（存储链先行，三星/SK海力士与 A 股半导体同频）
    gq = snapshot.get("global") or {}
    g_parts = []
    for g_name in ("纳斯达克100", "英伟达", "恒生科技指数", "韩国KOSPI"):
        g = gq.get(g_name) or {}
        if isinstance(g.get("change_pct"), (int, float)):
            g_parts.append(f"{g_name}{g['change_pct']:+.2f}%")
    if g_parts:
        parts.append("隔夜" + "、".join(g_parts) + "（AI硬件链/科技股先行指标）")
    vol_parts = []
    for v_name, v in (snapshot.get("vol") or {}).items():
        if isinstance(v, dict) and v.get("regime") == "高波":
            vol_parts.append(f"{v_name}高波（20日波动率{v.get('vol20', 0):.1f}%）")
    if vol_parts:
        parts.append("；".join(vol_parts) + "（机构降杠杆环境）")
    breadth = snapshot.get("breadth") or {}
    dp = breadth.get("down_pct")
    if isinstance(dp, (int, float)) and dp >= 80:
        ld = breadth.get("limit_down", 0)
        parts.append(f"极端普跌（{dp:.0f}%个股下跌，跌停{ld}家）")
    # P4（2026-08-19）：涨停情绪 + 行业资金流——LLM 判定短线情绪类事件的增量上下文
    sentiment = snapshot.get("sentiment") or {}
    if sentiment.get("mood") and sentiment.get("zt"):
        parts.append(f"涨停情绪{sentiment['mood']}"
                     f"（涨停{sentiment.get('zt', 0)}家，连板高度{sentiment.get('max_lbc', 0)}，"
                     f"炸板率{sentiment.get('zbr', 0):.0f}%）")
    sf = snapshot.get("sector_flows") or {}
    in_parts = [f"{n}{v:+.1f}亿" for n, v in (sf.get("inflow") or [])[:3]]
    out_parts = [f"{n}{v:+.1f}亿" for n, v in (sf.get("outflow") or [])[:3]]
    if in_parts:
        parts.append("主力净流入行业：" + "、".join(in_parts))
    if out_parts:
        parts.append("主力净流出行业：" + "、".join(out_parts))
    # P7（2026-08-19）：资金面利率 + 期权情绪——LLM 判定流动性敏感型事件
    # （货币政策/杠杆资金/利率类）与情绪敏感型事件的增量上下文
    liq = snapshot.get("liquidity") or {}
    gc = liq.get("gc007") or {}
    if isinstance(gc.get("price"), (int, float)):
        tight = gc["price"] >= 3.0
        parts.append(f"GC007资金面利率{gc['price']:.2f}%"
                     + ("（资金面收紧，杠杆资金承压）" if tight else "（资金面平稳）"))
    opt = snapshot.get("option") or {}
    if isinstance(opt.get("pcr"), (int, float)):
        pcr = opt["pcr"]
        mood = "恐慌对冲占优" if pcr >= 1.3 else ("看涨占优" if pcr <= 0.55 else "中性")
        parts.append(f"期权PCR{pcr:.2f}（机构情绪{mood}）")
    if not parts:
        return ""
    return f"（{ts}）" + "；".join(parts)


def _risk_off_downgrade(news: dict, judge: dict) -> bool:
    """风险收缩期降级判定：科技 bullish 且无硬事件佐证 → True（降级不推）

    规则：
    - 仅作用于 bullish 科技资讯（板块/个股级）；利空/中性/非科技不受影响
    - 硬事件佐证（任一即放行）：①具体金额（_extract_core_numbers）②百分比/倍数硬数据
      （如"良率98%""产能翻倍"）③完成态硬事件动词（中标/收购/扩建/投产等），
      且动词前无展望修饰（"有望/拟/验证窗口"等）
    - 纯情绪/板块利好（"景气提升""有望受益""空间广阔"等无事件佐证）→ 降级
    """
    if judge.get("direction") != "bullish":
        return False
    sectors = _as_list(judge.get("sectors"))
    # 科技识别三合一（宽松兜底 _is_tech_by_sectors，补 OVERSEAS_TECH_KEYWORDS 缺词）
    if not (_is_domestic_tech(news, sectors) or _is_overseas_tech(news, sectors)
            or _is_tech_by_sectors(sectors)):
        return False
    text = f"{str(news.get('title', ''))} {str(news.get('content', '') or '')[:200]}"
    # ① 具体金额 → 硬事件，放行
    if _extract_core_numbers(text):
        return False
    # ② 百分比/倍数硬数据（良率98%、市占率升至X%、产能翻倍）→ 放行
    if re.search(r"\d+(\.\d+)?%", text) or re.search(r"翻[一二两三四五六七八九十百\d]倍|倍增", text):
        return False
    # ③ 完成态硬事件动词（排除展望修饰）
    for w in _HARD_EVENT_WORDS:
        if w not in text:
            continue
        idx = text.find(w)
        ctx = text[max(0, idx - 6):idx]
        if any(m in ctx for m in _FORWARD_MODIFIERS):
            continue  # 展望性表述（"有望中标"），不算已发生
        return False
    return True


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

# P6（2026-08-19）跨事件叙事链：方向分组口径
_BULL_GROUP = {"bullish", "mildly_bullish"}
_BEAR_GROUP = {"bearish", "mildly_bearish"}


def _opposite_events_note(sig: dict, cur_dir: str, pushed_events: list, hours: int = 48) -> str:
    """跨事件矛盾检测（P6-2）：近 N 小时同主体反向已推事件

    叙事链的"矛盾"环节——今日利空事件 vs 48h 前同主体利多事件，推送时双向可见，
    用户看到完整叙事而非孤立判定（LLM 单条独立判定的补强）。返回附注文本，
    无匹配/方向中性返回 ""。
    """
    d = str(cur_dir or "")
    if d in _BULL_GROUP:
        opp, opp_label = _BEAR_GROUP, "利空"
    elif d in _BEAR_GROUP:
        opp, opp_label = _BULL_GROUP, "利多"
    else:
        return ""
    ents = {_normalize_entity(e) for e in _as_list(sig.get("stocks"))} | \
           {_normalize_entity(e) for e in _as_list(sig.get("entities"))}
    if not ents:
        return ""
    now_ts = time.time()
    hits = []
    for pe in pushed_events:
        if str(pe.get("dir") or "") not in opp:
            continue
        ts = _state_timestamp(pe.get("t"))
        if ts is None:
            continue
        if now_ts - ts > hours * 3600:
            continue
        pents = {_normalize_entity(e) for e in _as_list(pe.get("stocks"))} | \
                {_normalize_entity(e) for e in _as_list(pe.get("entities"))}
        shared = ents & pents
        # 2026-08-25 实证误配：英伟达产品发布（主实体=英伟达）挂上了"易中天三大
        # 巨头集体下跌"（英伟达只是文中偶然提及的第三实体，主实体是光模块三巨头），
        # 暗示不存在的叙事矛盾。收紧：共享实体须为对方事件的主实体（stocks∪首实体），
        # 或双方共享 ≥2 实体（强同主体）。偶然提及的上下文实体不再触发"反向"附注。
        if not shared:
            continue
        pe_primary = {_normalize_entity(e) for e in _as_list(pe.get("stocks"))}
        pe_ents = _as_list(pe.get("entities"))
        if pe_ents:
            pe_primary.add(_normalize_entity(pe_ents[0]))
        if not (shared & pe_primary) and len(shared) < 2:
            continue
        hits.append(f"{str(pe.get('title_norm') or '')[:40]}（{opp_label}）")
        if len(hits) >= 2:
            break
    if not hits:
        return ""
    return f"近{hours}h同主体反向已推事件：" + "、".join(hits)


def _related_recent_note(n: dict, pushed_events: list, hours: int = 48) -> list:
    """该条资讯的近 N 小时同主体已推事件（P6-3 叙事链：LLM 判定的增量上下文）

    判定时 items 尚无 _sig（签名依赖判定结果生成），按已推事件的主体词
    在标题+正文中的子串匹配。返回 ["标题（方向）", ...]（≤3 条），无匹配返回 []。
    """
    if not pushed_events:
        return []
    text = f"{n.get('title', '')} {n.get('content', '')}"
    now_ts = time.time()
    out = []
    for pe in pushed_events:
        # 2026-08-25 收紧（与 _opposite_events_note 同口径）：仅用对方事件的
        # 主实体（stocks∪首实体）匹配本文。此前全实体子串匹配会把"文中偶然
        # 提及的上下文实体"（如易中天事件里的英伟达）注入为"同主体反向叙事"，
        # prompt 又规定反向叙事可上调 score——造成弱关联虚加分。
        kws = [str(e) for e in (_as_list(pe.get("stocks")) + _as_list(pe.get("entities"))[:1])
               if str(e).strip()]
        if not any(k in text for k in kws):
            continue
        ts = _state_timestamp(pe.get("t"))
        if ts is None:
            continue
        if now_ts - ts > hours * 3600:
            continue
        label = _DIR_LABEL.get(str(pe.get("dir") or ""), "中性")
        out.append(f"{str(pe.get('title_norm') or '')[:36]}（{label}）")
        if len(out) >= 3:
            break
    return out


def format_push_alert(news: dict, judge: dict, factor_env: str = "", opposite_note: str = "") -> str:
    """格式化单条快讯为 markdown 文本（红涨绿跌）

    注: 企业微信 markdown 不支持 <font color> 内联 HTML，
    统一用 emoji + 文本标签表达方向（A股惯例红涨绿跌），
    PushPlus / 企业微信均能正确渲染。

    正文处理: 资讯原文 content 截断 300 字符附在推送里（避免只推标题+理由
    让用户看不到资讯内容），企业微信 4096 字节限制内有充足余量。

    factor_env（P0-1 2026-08-19 融合展示）: 由 _factor_env_line 生成的单行
    因子快照（"市场环境: ...IC贴水/日元/上证..."），空串时不附该行，
    向后兼容（factor_collector 未跑/快照过期 → 推送退化为原格式）。
    env_note（P2-2 2026-08-19，2026-08-22 停用展示）: judge 的共振/背离标注
    （LLM 结合量化环境生成，如"背离: 利好但风险收缩期，谨慎对待"）。已不再渲染到
    推送（用户决定——择时报告已覆盖量化环境判断）；LLM prompt/解析链路保留，
    需要时可恢复展示。
    opposite_note（P6-2 2026-08-19）: 跨事件矛盾附注（近48h同主体反向已推
    事件），空串不展示——叙事链的"矛盾"环节，双向可见。
    """
    title = str(news.get("title", "") or "")[:200]
    direction = judge.get("direction", "neutral")
    emoji = _DIR_EMOJI.get(direction, "⚪")
    label = _DIR_LABEL.get(direction, "中性")
    # 2026-08-13 P2 修复：LLM 返回 "score": null 时 e.get 命中 key 返回 None，
    # 直接 f-string 会显示 "影响分: None"；统一走 _to_float 兜底。
    score = _to_float(judge.get("score", 0))
    scope = judge.get("scope", "stock")
    scope_label = _SCOPE_LABEL.get(scope, "个股")
    sectors = _as_list(judge.get("sectors"))
    sector_str = "、".join(str(s) for s in sectors[:4]) if sectors else "—"
    reason = str(judge.get("reason", "") or "").strip()[:100]
    env_note = str(judge.get("env_note", "") or "").strip()[:60]
    source = str(news.get("source", "") or "多源资讯")
    pub = str(news.get("published_at", "") or "")
    content_text = str(news.get("content", "") or "").strip()

    lines = [f"{emoji}【{label}】{title}", ""]
    meta = [f"**范围**: {scope_label}", f"**影响分**: {score}", f"**板块**: {sector_str}"]
    lines.append(" | ".join(meta))
    if factor_env:
        lines.append(factor_env)
    if opposite_note:
        lines.append(f"**⚠️ {opposite_note}")
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
    """统一推送入口：按配置选择后端发送单条快讯，主后端失败时跨后端故障转移

    后端优先级: PushPlus > 企业微信群机器人
    成功判定: PushPlus code==200; 企业微信 errcode==0
    主后端失败且配置了备用后端时，自动转移到备用后端补齐（避免单后端故障丢推）。
    """
    backends = []
    if push_config.get("pushplus_token"):
        backends.append(("pushplus", lambda: push_via_pushplus(
            push_config["pushplus_token"], title, content)))
    if push_config.get("wecom_webhook"):
        backends.append(("wecom", lambda: push_via_wecom(
            push_config["wecom_webhook"], title, content)))
    if not backends:
        return {"code": 400, "msg": "未配置任何推送后端"}

    last = None
    for name, fn in backends:
        try:
            result = fn()
        except Exception as e:
            result = None
            logger.error(f"推送后端 {name} 异常: {e}")
        if result is None:
            result = {"code": -1, "errcode": -1, "msg": f"{name} 返回空/异常结果"}
        is_ok = (name == "pushplus" and result.get("code") == 200) or \
                (name == "wecom" and result.get("errcode") == 0)
        if is_ok:
            if last is not None:
                logger.info(f"推送故障转移成功: 主端失败后经 {name} 补齐")
            return result
        last = result
        logger.warning(f"推送后端 {name} 失败，尝试故障转移: {result}")
    return last


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

    # 2026-09-04 P1-1 修复：pending 条目主动重注入——滚出源窗口的挂起条目
    # 此前只能等重新被抓取（fp 相同）才重回判定，财联社 20 条滚动窗内溢出
    # 条目 1-2 轮后即永久躺 pending 到 48h 过期（实证：特斯拉 Cybercab retry=1
    # 挂 18h 从未进判定）。溢出时已存全量 payload，这里把本轮未重新抓到的
    # pending 条目按 retry 从大到小补注入候选流（老条目优先）。
    reinject = _reinject_pending_items(pending, news_list, seen)
    if reinject:
        new_items = reinject + new_items
        logger.info(f"pending 主动重注入 {len(reinject)} 条（滚出源窗口的挂起条目）")

    logger.info(f"增量检测: 新增 {len(new_items)} 条（已见 {len(news_list) - len(new_items)} 条）")
    if not new_items:
        logger.info("无新增资讯，本轮结束")
        if not dry_run:
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
    max_candidates = _env_int("RT_MAX_CANDIDATES", MAX_CANDIDATES_PER_ROUND)
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
                # 2026-09-04 P1-1：存全量 payload，滚出源窗口后下轮主动重注入
                pending[n["_fp"]] = {"t": now_for_pend, "retry": retry,
                                     "title": str(n.get("title", ""))[:60],
                                     "payload": _pend_payload(n)}
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
    # P2-2（2026-08-19）：LLM 判定注入量化环境上下文（风险状态/贴水/汇率/主力资金），
    # LLM 输出 env_note 共振/背离标注；factor_state 提前至此加载一次，
    # 下方 5c 的风险收缩期联动与市场环境行复用同一份（一次 Gist 读取三用）。
    factor_state = _load_factor_state()
    env_context = _llm_env_context(factor_state.get("snapshot") or {})
    # P6-3：已推事件提前加载（原 5c 处初始化上移）——LLM 叙事链上下文 + 5c 推送复用
    pushed_events = state.setdefault("pushed_events", [])
    _llm_deadline = time.monotonic() + 300
    judges = _llm_judge(candidates, deadline=_llm_deadline, env_context=env_context,
                        pushed_events=pushed_events)
    if not judges:
        # 防御：_llm_judge 异常返回空 → 全部挂起下轮重试（不推、不落指纹）
        judges = [_hang_judge(n) for n in candidates]

    # 自选龙头名单（watchlist.json），与 LLM 的 is_leader_stock 判定互为补充
    leader_watchlist = _load_leader_watchlist()
    if not leader_watchlist and not _watchlist_warned[0]:
        _watchlist_warned[0] = True
        logger.warning("自选龙头名单 watchlist.json 为空，龙头放行退化为 LLM is_leader_stock 单通道"
                       "（如需盘面异动龙头放行/科技龙头兜底双通道，请填入关注名单）")

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
                # 挂起条目（LLM 未判定，judged=False）不落 seen：若在此写
                # seen[pushed=False] 会违背"未判定不落指纹、下轮重试"核心策略——
                # 该源的这条资讯将永久不再被尝试判定/推送。
                if not _j.get("judged", True):
                    continue
                seen[n["_fp"]] = {"t": now, "pushed": False,
                                  "title": str(n.get("title", ""))[:52] + "[同事件合并]"}
                skipped += 1

    # 5c. 逐代表：LLM未判定挂起 → 强档方向门槛 → 阈值过滤 → 跨轮同事件拦截 → 推送
    # pushed_events 已在 LLM 判定前初始化（P6-3 上移）
    # 风险收缩期联动（2026-08-14 第二阶段）：factor_collector 写入的 risk_state，
    # risk_off 时对无硬事件佐证的科技利好降级不推。
    # 2026-08-22 停用：不再附"市场环境"因子快照行（用户决定——已上线创业板择时
    # 系统，14:30 择时报告含完整因子快照+仓位信号，资讯推送附该行冗余且时间戳过期）。
    # _factor_env_line 函数保留（纯函数+测试覆盖），需要时可恢复。
    risk_state = factor_state.get("risk_state")
    if risk_state not in ("risk_off", "neutral"):
        risk_state = "neutral"
    _cand_seen = set()  # P7-1：本轮已记录的候选 (日期,事件签名)，防同轮重复落 candidate_events
    for n, j in reps:
        if not j.get("judged", True):
            # 2026-08-03 用户口径：全部资讯必须经 LLM 判定。
            # 未判定条目不推、不落指纹 → 下轮重新送 LLM 判定（避免规则误判方向）。
            logger.info(f"LLM 未判定，挂起下轮重试: {n.get('title', '')[:40]}")
            skipped += 1
            continue
        # P7-1（2026-08-22）：预筛候选持久化——资讯维度可消费"当日全部重大候选"而非仅强档推送。
        # 每个经 LLM 判定的候选（含方向+事件签名）写入 candidate_events，今日作用域、按事件去重。
        # pushed 与否由 pushed_events 权威记录；此处统一量，供择时 news_modifier 并集读取。
        _dir0 = str(j.get("direction") or "")
        _ck = (now[:10], _event_sig_key(n.get("_sig") or {}))
        if _ck not in _cand_seen:
            _cand_seen.add(_ck)
            state.setdefault("candidate_events", []).append(
                {**n.get("_sig", {}), "dir": _dir0, "t": now, "pushed": False})
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
        # 2026-08-13 P0 修复：tech_override 排除研报/观点/主题类。
        # 强档门槛后 `score>=5 or strong` 恒真，tech_override 原本退化为
        # "scope=sector+科技词即放行"——"机构称物理AI"等研报/观点类在 LLM 判
        # push=false 后被强制放行（抵消 prompt 收紧，今日误推实证）。
        # 命中 _TECH_OVERRIDE_VIEW_WORDS（定性判断措辞）的科技消息不放行，
        # 让 LLM 的 push=false 生效；仅未命中观点词（可能被 LLM 漏判的硬事件）兜底放行。
        tech_override = _tech_override_enabled(n, j, leader_watchlist)
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

        # 风险收缩期降级（2026-08-14 第二阶段联动）：risk_off 时，科技利好若无硬事件
        # 佐证（金额/订单/公告/获批等）则降级不推——与量化资金风险期降杠杆一致；
        # 利空/风险资讯不受影响（风险期更应提示）。seen 标注原因便于复核。
        if risk_state == "risk_off" and _risk_off_downgrade(n, j):
            logger.info(f"风险收缩期降级(科技利好无硬事件佐证): {n.get('title', '')[:40]}")
            seen[n["_fp"]] = {"t": now, "pushed": False,
                              "title": str(n.get("title", ""))[:52] + "[风险收缩期降级]"}
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

        # P6-2：跨事件矛盾附注（近48h同主体反向已推事件）——叙事链"矛盾"环节
        opposite_note = _opposite_events_note(n["_sig"], str(j.get("direction") or ""),
                                              pushed_events)
        content = format_push_alert(n, j, opposite_note=opposite_note)
        if dry_run:
            logger.info(f"[dry-run] 将推送: {n.get('title', '')[:50]}")
            # Windows 控制台默认 GBK 无法打印 emoji，先切 UTF-8 容错
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
            print("\n===== 将推送内容预览 =====\n" + content + "\n==========================")
            seen[n["_fp"]] = {"t": now, "pushed": True, "title": str(n.get("title", ""))[:60]}
            # dir 字段（P0-3 2026-08-19）：盘后复盘按方向统计利多/利空占比。
            # 2026-09-01：补存原文 title（_sig 只有去标点 title_norm，可读性差），
            # 供盘后复盘列表统一以 pushed_events 为唯一数据源时直接展示。
            # 2026-09-04 P1-3：补存 source，供每源推送贡献率审计。
            pushed_events.append({**n["_sig"], "dir": j.get("direction"), "t": now,
                                  "title": str(n.get("title", ""))[:60],
                                  "source": str(n.get("source", "") or "")[:30]})
            pushed += 1
        else:
            # 推送标题直接用新闻原文标题（避免显示"重要资讯"占位符）
            push_title = str(n.get("title", "") or "")[:80] or "重大资讯"
            result = _send_alert_item(push_config, push_title, content)
            if result.get("code") == 200 or result.get("errcode") == 0:
                logger.info(f"推送成功: {n.get('title', '')[:50]}")
                seen[n["_fp"]] = {"t": now, "pushed": True, "title": str(n.get("title", ""))[:60]}
                pushed_events.append({**n["_sig"], "dir": j.get("direction"), "t": now,
                                      "title": str(n.get("title", ""))[:60],
                                      "source": str(n.get("source", "") or "")[:30]})
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
        else:
            # 2026-08-25 审核实证：同事件已推送但 pending 记录因措辞/来源不同
            #（fp 不同）无法被上文清理，持续无限重试（华为 retry=4/Meta retry=6）。
            # 兜底：其标题与某条已推事件标题高度相似 → 视为已推同事件，移除。
            rec = pending[fp]
            pt = str(rec.get("title") or "")
            if pt and any(_pending_same_as_pushed(pt, pe.get("title_norm") or "")
                          for pe in pushed_events):
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


# ============================================================
# P0-2/P0-3 盘前简报 + 盘后复盘（融合展示，2026-08-19）
# ============================================================
# 隔夜窗口：昨日收盘 15:00（BJT）之后发布的资讯都算"隔夜/盘前要闻"
_OVERNIGHT_SINCE_HOUR = 15
# 盘前简报要闻条数上限（信息密度优先，超出的按预筛分截断）
BRIEF_TOP_N = 5

_PUB_TIME_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                     "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                     "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"]


def _parse_pub_time(text: str):
    """解析 published_at 为 naive BJT datetime；失败返回 None（调用方保留该条不误删）"""
    text = str(text or "").strip()
    for fmt in _PUB_TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# 板块标签归一规则：有序，越具体越靠前，一个标签只归入一个规范类（防重复计数）。
# 2026-09-01 审核实证：LLM 抽的 sectors 是自由文本，'半导体'/'半导体设备'/
# '半导体/HBM'/'半导体/晶圆代工' 各计 1 票 → Top3 退化为并列 2 票的噪声。
_SECTOR_CANON_RULES = (
    ("存储芯片", ("存储芯片", "半导体/存储芯片", "hbm", "dram", "nand", "闪存", "存储器")),
    ("光模块", ("光模块", "cpo", "光通信")),
    ("AI算力", ("ai算力", "ai/算力", "ai芯片", "ai硬件", "算力", "云计算",
                "服务器", "数据中心", "idc")),
    ("半导体", ("半导体", "晶圆", "国产替代", "芯片", "封测", "光刻", "设备")),
    ("能源", ("能源", "原油", "石油", "油气", "石化", "煤炭", "天然气")),
    ("宏观", ("宏观", "利率", "汇率", "美联储", "央行", "cpi", "pmi")),
    ("电子元件", ("mlcc", "电子元件", "被动元件", "电容")),
    ("港股", ("中概股", "港股")),
    ("化工", ("化工", "化学")),
    ("制造业", ("制造业", "工业")),
)


def _canonical_sector(raw) -> str:
    """把 LLM 自由板块标签归一到规范名；空/无效标签返回 ''。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    low = s.lower()
    for canon, keys in _SECTOR_CANON_RULES:
        if any(k in low for k in keys):
            return canon
    return s


def _direction_asof(factor_state: dict) -> str:
    """量化方向结论的归属日期（YYYY-MM-DD）；无 direction_history 时返回 ''。

    2026-09-01：direction_history 以日期为键，最新键即最近一次打分的交易日。
    last_direction 只是值，本身不带时间，必须回查 history 才能判断时效。
    """
    hist = factor_state.get("direction_history") or {}
    if not isinstance(hist, dict) or not hist:
        return ""
    keys = [str(k) for k in hist.keys() if str(k).strip()]
    return max(keys) if keys else ""


def _stale_trading_days(asof: str, today: str, cap: int = 15) -> int:
    """asof 之后（不含当日）到 today 之间新增的交易日数。

    返回 -1 表示日期无法解析/asof 缺失。交易日口径复用 _is_trading_day
    （中国工作日，chinese_calendar 缺失时退化为周一至周五）。
    """
    try:
        d0 = datetime.strptime(str(asof)[:10], "%Y-%m-%d").date()
        d1 = datetime.strptime(str(today)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return -1
    if d1 <= d0:
        return 0
    n = 0
    cur = d0 + timedelta(days=1)
    while cur <= d1 and n < cap:
        try:
            if _is_trading_day(cur):
                n += 1
        except Exception:
            pass
        cur += timedelta(days=1)
    return n


def _snapshot_block(factor_state: dict, title: str) -> list:
    """把 factor_collector 的紧凑快照格式化为 markdown 块（盘前/盘后共用）"""
    snap = factor_state.get("snapshot") or {}
    lines = [f"### {title}", ""]
    if not snap:
        lines.append("- 因子快照缺失（factor_collector 未运行或首次部署），仅资讯维度")
        return lines
    ts = str(snap.get("ts", "") or "")
    risk = str(snap.get("risk_state", "") or "")
    risk_txt = "⚠️ 风险收缩期" if risk == "risk_off" else "中性"
    lines.append(f"- 风险状态: {risk_txt}（更新 {ts}）")
    for name in ("上证指数", "创业板指"):
        idx = (snap.get("indexes") or {}).get(name) or {}
        if idx:
            arrow = "▲" if idx.get("change_pct", 0) >= 0 else "▼"
            lines.append(f"- {name}: {arrow} {idx.get('change_pct', 0):+.2f}%"
                         f"（{idx.get('price', 0):.2f}，{idx.get('trend', '')}）")
    basis = snap.get("basis") or {}
    if basis:
        parts = []
        for code in ("IF", "IC", "IM", "IH"):
            b = basis.get(code)
            if isinstance(b, dict) and isinstance(b.get("basis_pct"), (int, float)):
                tag = "贴水" if b["basis_pct"] < 0 else "升水"
                parts.append(f"{code} {tag}{abs(b['basis_pct']):.2f}%"
                             f"(年化{b.get('annual_pct', 0):+.1f}%)")
        if parts:
            lines.append("- 股指期货: " + "｜".join(parts))
    fx = snap.get("fx") or {}
    fx_parts = []
    for label in ("美元/日元", "美元/在岸人民币"):
        f = fx.get(label) or {}
        if isinstance(f.get("change_pct"), (int, float)):
            fx_parts.append(f"{label} {f['price']:.4f}({f['change_pct']:+.2f}%)")
    if fx_parts:
        lines.append("- 汇率: " + "｜".join(fx_parts))
    # P3（2026-08-19）：隔夜外盘 / 宽度 / 波动率 / 风格
    gq = snap.get("global") or {}
    g_parts = []
    for g_name in ("纳斯达克100", "标普500", "英伟达", "恒生科技指数"):
        g = gq.get(g_name) or {}
        if isinstance(g.get("change_pct"), (int, float)):
            arrow = "▲" if g["change_pct"] >= 0 else "▼"
            warn = "⚠️" if abs(g["change_pct"]) >= 2.0 else ""
            g_parts.append(f"{g_name}{warn}{arrow}{g['change_pct']:+.2f}%")
    if g_parts:
        lines.append("- 隔夜外盘: " + "｜".join(g_parts))
    breadth = snap.get("breadth") or {}
    if breadth.get("down_pct") is not None:
        lines.append(f"- 市场宽度: 涨{breadth.get('adv', 0)}/跌{breadth.get('dec', 0)}"
                     f"（{breadth.get('down_pct', 0):.0f}%下跌）"
                     f"｜涨停{breadth.get('limit_up', 0)}/跌停{breadth.get('limit_down', 0)}"
                     f"｜跌超5% {breadth.get('big_down', 0)}")
    vol = snap.get("vol") or {}
    vol_parts = []
    for v_name, v in vol.items():
        if isinstance(v, dict):
            vol_parts.append(f"{v_name} {v.get('vol20', 0):.1f}%"
                             f"({v.get('pctile', 0):.0f}分位{'高波' if v.get('regime') == '高波' else v.get('regime', '')})")
    if vol_parts:
        lines.append("- 波动率: " + "｜".join(vol_parts))
    style = snap.get("style") or {}
    if style.get("trend"):
        lines.append(f"- 风格轮动: {style.get('trend')}"
                     f"（50/1000比价20日{style.get('chg20', 0):+.1f}%）")
    # P4（2026-08-19）：涨停情绪 + 行业资金流
    sentiment = snap.get("sentiment") or {}
    if sentiment.get("zt"):
        mood = sentiment.get("mood", "")
        flag = "🔥" if mood == "亢奋" else ("❄️" if mood == "冰点" else "")
        lines.append(f"- 涨停情绪: {flag}{mood}｜涨停{sentiment.get('zt', 0)}"
                     f"（连板高度{sentiment.get('max_lbc', 0)}）"
                     f"｜炸板率{sentiment.get('zbr', 0):.0f}%")
    # 2026-09-01：资金维度全部显式标注来源成败。审核实证：全市场净流出 309 亿
    # 未进复盘，而行业资金流源已连失败 41 轮仍在展示残缺数据 → 信息严重偏斜。
    # 原则：有数据就展示，无数据必须写"数据缺失"，绝不整行静默消失或补零。
    failed_srcs = [str(x) for x in ((snap.get("sources") or {}).get("failed") or [])]
    mflow = snap.get("flows") or {}
    mn = mflow.get("main_net_yi")
    mgy = mflow.get("margin_yi")
    mf_parts = []
    if isinstance(mn, (int, float)):
        m_arrow = "▲" if mn >= 0 else "▼"
        mf_parts.append(f"{m_arrow} 主力净{'流入' if mn >= 0 else '流出'} {abs(mn):.0f} 亿")
    elif "资金流" in failed_srcs:
        mf_parts.append("主力净流入 数据缺失")
    if isinstance(mgy, (int, float)):
        mf_parts.append(f"两融 {mgy:.0f} 亿（{mflow.get('margin_chg_yi', 0):+.0f}）")
    if mf_parts:
        lines.append("- 资金流向: " + "｜".join(mf_parts))
    sf = snap.get("sector_flows") or {}
    in_s = "、".join(f"{n} {v:+.1f}亿" for n, v in (sf.get("inflow") or [])[:3])
    out_s = "、".join(f"{n} {v:+.1f}亿" for n, v in (sf.get("outflow") or [])[:3])
    if in_s or out_s:
        # 流出空不再显示裸"—"（8-31 审核实证"流出 —"无法区分真无流出 vs
        # 数据缺半边）。fetch_sector_flows 有行数守卫，能产出 inflow 非空 +
        # outflow 空的组合即"全部返回行业主力净流入≥0"这一合法市场事实。
        out_show = out_s or ("无净流出行业" if in_s else "—")
        lines.append(f"- 行业资金: 流入 {in_s or '—'} ｜ 流出 {out_show}")
    elif "行业资金流" in failed_srcs:
        lines.append("- 行业资金: 数据缺失（源失败已整弃，≠无资金异动）")
    # P7（2026-08-19）：资金面利率 + 期权情绪（影子因子，简报常规展示）
    liq = snap.get("liquidity") or {}
    gc = liq.get("gc007") or {}
    if isinstance(gc.get("price"), (int, float)):
        tight = gc["price"] >= 3.0
        flag = "⚠️" if tight else ""
        gc1 = liq.get("gc001") or {}
        gc1_s = (f"｜GC001 {gc1['price']:.2f}%"
                 if isinstance(gc1.get("price"), (int, float)) else "")
        lines.append(f"- 资金面利率: {flag}GC007 {gc['price']:.2f}%{gc1_s}"
                     + ("（资金面收紧）" if tight else "（平稳）"))
    opt = snap.get("option") or {}
    if isinstance(opt.get("pcr"), (int, float)):
        pcr = opt["pcr"]
        flag = "⚠️" if pcr >= 1.5 else ""
        mood = ("恐慌对冲占优" if pcr >= 1.3 else
                "看涨情绪占优" if pcr <= 0.55 else "情绪中性")
        lines.append(f"- 期权情绪: {flag}PCR {pcr:.2f}（{mood}）"
                     f"｜认购 {opt.get('call_vol', 0)} / 认沽 {opt.get('put_vol', 0)} 张")
    # P5-2：盘中弱翻转提示（未达强信号门槛不单独推，简报兜底可见）
    weak = factor_state.get("weak_direction") or {}
    if weak.get("dir") and weak.get("dir") != factor_state.get("last_direction"):
        lines.append(f"- 盘中弱信号: {weak.get('dir')}（{weak.get('score', 0):+.2f}，"
                     f"未达强信号门槛，仅供参考）")
    # P5-3：数据健康度（源成功率）
    src = snap.get("sources") or {}
    if src.get("total"):
        ratio = src.get("ok", 0) / src["total"]
        flag = "⚠️" if ratio < 0.7 else ""
        # 2026-09-01：附异常源名单（8-31 审核实证：12/14 但不知哪 2 个源失败）
        failed = src.get("failed") or []
        tag = f"（异常：{'、'.join(str(x) for x in failed)}）" if failed else ""
        lines.append(f"- 数据健康度: {flag}{src.get('ok', 0)}/{src['total']} 源正常{tag}")
    last_dir = str(factor_state.get("last_direction", "") or "")
    if last_dir:
        # P4-6：IC 加权已启用时标注（含样本数），人工决策时知悉合成口径
        ic = factor_state.get("factor_ic") or {}
        ic_tag = ""
        if ic.get("weights"):
            n = ic.get("n")
            ic_tag = f"（IC加权，n={n}）" if isinstance(n, int) else "（IC加权）"
        # 2026-09-01 方向停更保护（审核实证：9-01 盘后展示的"偏多"实为 8-28
        # 打分，与当日"创业板指 空头排列"反向，却无任何时效标注 → 误导决策）。
        # 方向结论必须自带日期；停更 ≥1 个交易日即降级，禁止冒充当日结论。
        today_str = datetime.now(BJT).strftime("%Y-%m-%d")
        asof = _direction_asof(factor_state)
        stale = _stale_trading_days(asof, today_str) if asof else -1
        if stale < 0:
            lines.append(f"- 量化综合方向: ⚠️ {last_dir}（打分日期未知："
                         "factor_collector 未记录 direction_history，勿作当日结论）")
        elif stale >= 1:
            lines.append(f"- 量化综合方向: ⚠️ 待更新（末次 {asof}「{last_dir}」，"
                         f"已停更 {stale} 个交易日，非当日结论）")
        else:
            lines.append(f"- 量化综合方向: {last_dir}{ic_tag}")
    return lines


def _push_config_from_env() -> dict:
    """从环境变量构建推送配置（与 run_once 同口径）"""
    return {
        "pushplus_token": os.getenv("PUSHPLUS_TOKEN", "").strip() or None,
        "wecom_webhook": os.getenv("WECOM_WEBHOOK", "").strip() or None,
    }


def run_morning_brief(dry_run: bool = False) -> dict:
    """盘前简报（08:45 触发）：隔夜要闻 Top5 + 因子环境 + 自选关注

    只读设计：不调用 LLM、不写状态（不落 seen/pushed_events）——
    隔夜资讯仍由常规 30 分钟轮询正常判定推送，简报只是提前聚合预览。
    """
    now = datetime.now(BJT)
    factor_state = _load_factor_state()

    lines = [f"## 📋 盘前简报 {now.strftime('%Y-%m-%d %H:%M')}", ""]

    # 1. 隔夜要闻 Top5（预筛分排序，带规则方向提示；无 LLM 判定，人工把关）
    lines.append("### 隔夜要闻（按重要度 Top5）")
    lines.append("")
    try:
        news_list = list(get_stock_news.func())
    except Exception as e:
        logger.error(f"盘前简报新闻抓取失败: {e}")
        news_list = []
    try:
        signals = list(get_market_signals.func())
    except Exception:
        signals = []
    news_list = dedup_news_3layer(news_list) + signals

    since = (now - timedelta(days=1)).replace(hour=_OVERNIGHT_SINCE_HOUR, minute=0,
                                              second=0, microsecond=0)
    overnight = []
    for n in news_list:
        pt = _parse_pub_time(n.get("published_at"))
        if pt is not None and pt < since.replace(tzinfo=None):
            continue
        score, hit = _prefilter(n)
        overnight.append((score, bool(hit), n))
    overnight.sort(key=lambda x: (x[1], x[0]), reverse=True)
    if overnight:
        # 信号类（龙虎榜/业绩预告，交易所结构化数据）预筛分恒定 0.75，
        # 会挤占隔夜宏观/事件要闻席位——简报是"事件综述"，信号最多占 2 席
        signal_slots = 2
        shown = 0
        for score, hit, n in overnight:
            title = str(n.get("title", "") or "")
            is_signal = title.startswith("龙虎榜:") or title.startswith("业绩预告:")
            if is_signal:
                if signal_slots <= 0:
                    continue
                signal_slots -= 1
            if shown >= BRIEF_TOP_N:
                break
            shown += 1
            source = str(n.get("source", "") or "")
            direction = predict_direction_by_rules(title[:60], str(n.get("content", "") or ""))
            emoji = _DIR_EMOJI.get(direction, "⚪")
            star = "⚡" if hit else ""
            lines.append(f"{shown}. {emoji} {title[:60]}（{source}，重要度{score:.2f}）{star}")
    else:
        lines.append("- 隔夜无重要资讯")
    lines.append("")

    # 2. 因子环境（来自 factor_collector 最近一轮快照）
    lines.extend(_snapshot_block(factor_state, "因子环境"))
    lines.append("")

    # 3. 自选关注（P1-2：附最近一轮快照行情——08:45 盘前为昨日收盘口径）
    watchlist = _load_leader_watchlist()
    if watchlist:
        lines.append("### 自选关注（最近快照）")
        lines.append("")
        stocks_snap = (factor_state.get("snapshot") or {}).get("stocks") or {}
        for name in sorted(watchlist):
            s = stocks_snap.get(name) or {}
            if isinstance(s.get("change_pct"), (int, float)) and s.get("price"):
                arrow = "▲" if s["change_pct"] >= 0 else "▼"
                lines.append(f"- {name} {s['price']} {arrow}{s['change_pct']:+.2f}%")
            else:
                lines.append(f"- {name}")
        lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    stats = {"overnight_candidates": len(overnight), "pushed": 0}

    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        print("\n===== 盘前简报预览 =====\n" + content + "\n========================")
        return stats
    push_config = _push_config_from_env()
    if not push_config["pushplus_token"] and not push_config["wecom_webhook"]:
        raise RuntimeError("盘前简报推送失败: 未配置 PUSHPLUS_TOKEN 或 WECOM_WEBHOOK")
    result = _send_alert_item(push_config, f"盘前简报 {now.strftime('%m-%d %H:%M')}", content)
    if result.get("code") == 200 or result.get("errcode") == 0:
        stats["pushed"] = 1
        logger.info("盘前简报推送成功")
    else:
        logger.error(f"盘前简报推送失败: {result}")
    return stats


def run_evening_review(dry_run: bool = False, on_date: str = None) -> dict:
    """盘后复盘（15:10 触发）：当日已推事件回顾 + 推送统计 + 因子环境

    只读设计：读 state 不写 state（当日数据由常规轮询维护，复盘仅聚合展示）。
    on_date（2026-09-02）：补发指定日期复盘（如凌晨补发前一交易日），
    默认当天。日期仅过滤事件列表，因子环境始终取 factor_state 最新快照。
    """
    now = datetime.now(BJT)
    today = on_date or now.strftime("%Y-%m-%d")
    state = load_state()
    factor_state = _load_factor_state()

    lines = [f"## 📊 盘后复盘 {today}", ""]

    # 1. 当日已推事件（唯一数据源：pushed_events）
    # 2026-09-01 口径统一：此前事件列表取 seen、方向分布取 pushed_events，
    # 两者清理策略不同（SEEN_MAX 条 vs 300 条，均 48h 窗口）必然漂移——
    # 9-01 实锤：列表 28 条 / 方向分布 29 条，差的那条正是被 seen 容量挤出、
    # 只存在于 pushed_events 的 00:32「君正股份 DRAM 涨价」。
    # 现同一份 today_events 同时供列表与方向分布使用，杜绝双口径。
    today_events = [pe for pe in (state.get("pushed_events") or [])
                    if str(pe.get("t", "")).startswith(today)]
    todays = sorted(today_events, key=lambda r: str(r.get("t", "")))
    lines.append(f"### 今日已推事件（{len(todays)} 条）")
    lines.append("")
    if todays:
        for rec in todays:
            t = str(rec.get("t", ""))[11:16]
            # 2026-09-01 起的新条目带原文 title；历史条目回落到 title_norm
            title = str(rec.get("title") or rec.get("title_norm") or "")[:60]
            lines.append(f"- {t} {title}")
    else:
        lines.append("- 今日无推送（平静日或系统未运行）")
    lines.append("")

    # 2. 推送方向统计（与事件列表同源：pushed_events 当日条目，P0-3 起带 dir）
    bull = sum(1 for pe in today_events if pe.get("dir") == "bullish")
    bear = sum(1 for pe in today_events if pe.get("dir") == "bearish")
    if today_events:
        lines.append(f"### 方向分布")
        lines.append("")
        lines.append(f"- 利好 {bull} 条｜利空 {bear} 条｜未标注 {len(today_events) - bull - bear} 条")
        # 板块热度 Top3（2026-09-01：先归一再计数，同分用板块名做二级排序键。
        # 此前 sorted(key=count, reverse=True) 在并列 2 票时按 dict 插入顺序取
        # 前三，Top3 实际是任意的，掩盖了"半导体/AI算力各 7 票"的真实主线）
        sector_count = {}
        for pe in today_events:
            for s in _as_list(pe.get("sectors")):
                canon = _canonical_sector(s)
                if canon:
                    sector_count[canon] = sector_count.get(canon, 0) + 1
        top_sectors = sorted(sector_count.items(), key=lambda x: (-x[1], x[0]))[:3]
        if top_sectors:
            lines.append("- 板块热度: " + "、".join(f"{s}({c})" for s, c in top_sectors))
        lines.append("")

    # 3. 因子环境
    lines.extend(_snapshot_block(factor_state, "收盘因子环境"))
    lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    stats = {"pushed_events": len(todays), "pushed": 0}

    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        print("\n===== 盘后复盘预览 =====\n" + content + "\n========================")
        return stats
    push_config = _push_config_from_env()
    if not push_config["pushplus_token"] and not push_config["wecom_webhook"]:
        raise RuntimeError("盘后复盘推送失败: 未配置 PUSHPLUS_TOKEN 或 WECOM_WEBHOOK")
    result = _send_alert_item(push_config, f"盘后复盘 {today}", content)
    if result.get("code") == 200 or result.get("errcode") == 0:
        stats["pushed"] = 1
        logger.info("盘后复盘推送成功")
    else:
        logger.error(f"盘后复盘推送失败: {result}")
    return stats


def _is_trading_day(day=None) -> bool:
    """A股交易日判断（RT_ALWAYS_ON=0 时启用）。"""
    from datetime import date
    return _is_workday(day or datetime.now(BJT).date(), "cn")


def main():
    parser = argparse.ArgumentParser(description="实时重要资讯推送")
    parser.add_argument("--loop", action="store_true", help="常驻循环模式（本地守护进程）")
    parser.add_argument("--dry-run", action="store_true", help="只诊断不推送不保存状态")
    parser.add_argument("--brief", choices=["morning", "evening"],
                        help="盘前简报(morning, 08:45)/盘后复盘(evening, 15:10)，融合因子快照（P0-2/P0-3）")
    parser.add_argument("--date", default=None,
                        help="补发指定日期的复盘（YYYY-MM-DD，与 --brief evening 搭配；默认当天）")
    args = parser.parse_args()

    # 2026-08-13 P2 修复：删除重复 basicConfig——src.config 已配置 root logger
    # （StreamHandler + logs/agent.log FileHandler），此处重复调用为 no-op 且配置不一致。
    # 仅当 root 无 handler 时兜底配置（如第三方以最小方式导入本模块）。

    always_on = os.getenv("RT_ALWAYS_ON", "1").strip() == "1"
    if not always_on and not _is_trading_day():
        logger.info("今日非A股交易日（RT_ALWAYS_ON=0），跳过")
        return

    if args.brief:
        # 盘前简报/盘后复盘：独立入口，只读不写状态（不影响常规轮询去重）
        if args.brief == "morning":
            run_morning_brief(dry_run=args.dry_run)
        else:
            run_evening_review(dry_run=args.dry_run, on_date=args.date)
        return

    if args.loop:
        poll_seconds = _env_int("RT_POLL_SECONDS", 120)
        logger.info(f"实时推送守护进程启动，轮询间隔 {poll_seconds}s（Ctrl+C 退出）")
        gist_token, gist_id = get_gist_config()
        if gist_token and gist_id:
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
